from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .services.job_admission import TERMINAL_JOB_STATUSES, lock_vault
from .services.s3_copy import storage_class_requires_restore
from .services.protection_states import (
    VISIBLE_SQL, LATEST_JOIN_SQL, aggregate_state, path_excluded_sql, project_file, state_sql,
)
from .services.directory_aggregates import (
    count_child_directories,
    ensure_directory_aggregates,
    invalidate_for_confirmed_rename,
    list_child_directory_rows,
    local_copy_is_upload_eligible,
    mark_directory_dirty,
    mark_file_id_dirty,
    mark_path_dirty,
    mark_paths_dirty,
    request_vault_rebuild,
)


class VaultFileNotFound(LookupError):
    """A Vault File could not be resolved in the expected Vault."""


class ReplacementInProgress(RuntimeError):
    """The Vault File already has its one replacement in progress."""


class StaleReplacement(LookupError):
    """The named Archive Version is no longer this file's replacement in progress."""


AUTO_RENAME_WINDOW = timedelta(hours=1)

# A Job holds its File until it ends. A pending Delayed Cloud Deletion is only
# the Owner's consent: a reappearance or a rename revokes it (#587, #594).
_OPEN_JOB_SQL = "status NOT IN ('completed', 'failed', 'cancelled')"
_DELETION_CONSENT_SQL = "action='cloud-purge' AND status='pending_delay'"

# Columns of one Vault File row, in the order ``_file_dict`` reads them.
_FILE_COLUMNS_SQL = """
    f.id, f.vault_id, f.path, f.path_since, f.generation,
    f.local_presence, f.local_file_type, f.local_size, f.local_mtime_ns,
    f.local_sha256, f.local_seen_at, f.local_observed_at,
    f.local_content_observed_at, f.local_recovered_at,
    f.matched_version_id, f.held_version_id, f.pending_since,
    f.reconciliation_block, f.reconciliation_blocked_at, f.cloud_since,
    f.current_version_id, f.replacing_version_id, f.created_at,
    cur.object_key AS cur_object_key,
    cur.provider_version_id AS cur_provider_version_id,
    cur.size AS cur_size, cur.storage_class AS cur_storage_class,
    cur.plaintext_sha256 AS cur_plaintext_sha256, cur.integrity AS cur_integrity,
    cur.availability AS cur_availability, cur.verified_at AS cur_verified_at,
    cur.created_at AS cur_created_at,
    rep.object_key AS rep_object_key,
    rep.provider_version_id AS rep_provider_version_id,
    rep.size AS rep_size, rep.storage_class AS rep_storage_class,
    rep.plaintext_sha256 AS rep_plaintext_sha256, rep.integrity AS rep_integrity,
    rep.availability AS rep_availability, rep.verified_at AS rep_verified_at,
    rep.created_at AS rep_created_at
"""
_FILE_JOINS_SQL = """
    LEFT JOIN archive_versions cur ON cur.id = f.current_version_id
    LEFT JOIN archive_versions rep ON rep.id = f.replacing_version_id
"""


def _only_cloud_copy_sql(file_alias: str) -> str:
    """The verified, available Current a reappearing Local Copy must respect.

    A Local Copy that was absent (freed, deleted via SMB, never restored after
    a catalog rebuild) leaves Current as the only copy. Content that reappears
    at the path stays held back until it proves identical or the Owner
    confirms the replacement (#411, #363 D7). Current is the explicit pointer,
    never the latest version by ordering.
    """
    return f"""(
        SELECT only_copy.id FROM archive_versions only_copy
        WHERE only_copy.id = {file_alias}.current_version_id
          AND only_copy.integrity = 'verified'
          AND only_copy.availability = 'available'
    )"""


def _timestamp(value: Any) -> datetime | None:
    """Convert a boundary timestamp (ISO text or datetime) to an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _required_timestamp(value: Any) -> datetime:
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("A timestamp is required")
    return parsed


def _digest(value: str | bytes) -> bytes:
    """A SHA-256 digest from hex text or raw bytes, validated once here."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    else:
        text = str(value)
        if len(text) != 64:
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        raw = bytes.fromhex(text)
    if len(raw) != 32:
        raise ValueError("SHA-256 must be 32 bytes")
    return raw


def _hex(value: Any) -> str | None:
    return None if value is None else bytes(value).hex()


def _id(value: Any) -> str | None:
    return None if value is None else str(value)


def _within_rename_window(left: Any, right: Any) -> bool:
    start = _timestamp(left)
    end = _timestamp(right)
    if start is None or end is None:
        return False
    return abs(start - end) <= AUTO_RENAME_WINDOW


def _version_dict(row: Mapping[str, Any], prefix: str, version_id: Any) -> dict[str, Any] | None:
    if version_id is None:
        return None
    return {
        "id": str(version_id),
        "object_key": row[f"{prefix}_object_key"],
        "provider_version_id": row[f"{prefix}_provider_version_id"],
        "size": row[f"{prefix}_size"],
        "storage_class": row[f"{prefix}_storage_class"],
        "plaintext_sha256": _hex(row[f"{prefix}_plaintext_sha256"]),
        "integrity": row[f"{prefix}_integrity"],
        "availability": row[f"{prefix}_availability"],
        "verified_at": row[f"{prefix}_verified_at"],
        "created_at": row[f"{prefix}_created_at"],
    }


def _file_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """One Vault File with its explicit states, converted once at the boundary."""
    local_copy = None
    if row["local_presence"] is not None:
        local_copy = {
            "presence": row["local_presence"],
            "file_type": row["local_file_type"],
            "size": row["local_size"],
            "mtime_ns": row["local_mtime_ns"],
            "plaintext_sha256": _hex(row["local_sha256"]),
            "seen_at": row["local_seen_at"],
            "observed_at": row["local_observed_at"],
            "content_observed_at": row["local_content_observed_at"],
            "recovered_at": row["local_recovered_at"],
        }
    block = None
    if row["reconciliation_block"] is not None:
        block = {
            "reason": row["reconciliation_block"],
            "blocked_at": row["reconciliation_blocked_at"],
        }
    return {
        "id": str(row["id"]),
        "vault_id": int(row["vault_id"]),
        "path": row["path"],
        "path_since": row["path_since"],
        "generation": int(row["generation"]),
        "local_copy": local_copy,
        "current_version": _version_dict(row, "cur", row["current_version_id"]),
        "replacing_version": _version_dict(row, "rep", row["replacing_version_id"]),
        "matched_version_id": _id(row["matched_version_id"]),
        "held_version_id": _id(row["held_version_id"]),
        "pending_since": row["pending_since"],
        "reconciliation_block": block,
        "cloud_since": row["cloud_since"],
        "created_at": row["created_at"],
    }


class ArchiveCatalog:
    """Keep Vault File invariants behind one persistence interface.

    One ``files`` row per Vault File holds its current path, Local Copy and
    explicit protection state (Current, replacement in progress, matched,
    held, pending, reconciliation block); ``file_path_history`` keeps only the
    closed paths (#587, ADR-0035). The methods below that still read the
    v0.5 tables (``vault_files``, ``local_copies``, ``file_paths``, version
    numbers) belong to the consumers ported by later issues of epic #578:
    upload versions and Job targets (#588/#590), listing, search and summary
    projections (#593), claims and queueing (#589), restore placement (#594)
    and storage-class copies (#595).
    """

    def __init__(self, connection: Any, *, protection_unconfirmed: bool = False):
        self.connection = connection
        self.protection_unconfirmed = protection_unconfirmed
        self.last_skipped_same_class = 0
        self.last_listing_rows_materialized = 0

    # -- shared seams -------------------------------------------------------

    def _revoke_deletion_consents(
        self, vault_id: int, file_ids: Sequence[Any], at: Any, *, reason: str
    ) -> None:
        """Permanently revoke pending Delayed Cloud Deletions of these files.

        The whole selection is revoked, never left partially authorized: a
        reappearance or rename is new evidence the consent never covered.
        Callers already hold the Vault lock.
        """
        ids = [str(file_id) for file_id in file_ids if file_id]
        if not ids:
            return
        stamp = _required_timestamp(at)
        self.connection.execute(
            f"""
            UPDATE jobs
            SET status='cancelled', message=%s, updated_at=%s, finished_at=%s
            WHERE vault_id=%s AND {_DELETION_CONSENT_SQL}
              AND (
                  file_id = ANY(%s::uuid[])
                  OR group_id IN (
                      SELECT consent.group_id FROM jobs consent
                      WHERE consent.vault_id=%s
                        AND consent.file_id = ANY(%s::uuid[])
                        AND consent.action='cloud-purge'
                        AND consent.status='pending_delay'
                  )
              )
            """,
            (reason, stamp, stamp, vault_id, ids, vault_id, ids),
        )

    def _mark_path_aggregates_dirty(self, vault_id: int, path: str | None) -> None:
        mark_path_dirty(self.connection, vault_id, path)

    def _mark_file_aggregates_dirty(
        self, vault_id: int, vault_file_id: str | None
    ) -> None:
        mark_file_id_dirty(self.connection, vault_id, vault_file_id)

    def _request_aggregate_rebuild(self, vault_id: int) -> None:
        request_vault_rebuild(self.connection, vault_id)

    def _vault_lock_touch(self, vault_id: int) -> None:
        # Serialize identity creation per Vault across scanner and worker
        # transactions, and keep the repository-wide lock order: Vault row
        # first, then file rows. Taken once per batch, never once per file.
        self.connection.execute(
            "UPDATE vaults SET name=name WHERE id=%s",
            (vault_id,),
        )

    # -- Local Copy observation ---------------------------------------------

    def observe_local_copy(
        self,
        *,
        vault_id: int,
        path: str,
        file_type: str,
        size: int | None,
        mtime_ns: int | None,
        observed_at: Any,
        seen_at: Any | None = None,
        recovered_at: Any | None = None,
        invalidate_content: bool = False,
    ) -> str:
        """Record the observed Local Copy at ``path`` and return its file id.

        Every observation increments the file's generation, even with the same
        stat, so a hash taken on an earlier observation can no longer publish.
        The fingerprint and match are kept only for a present regular file
        whose stat is unchanged, unless ``invalidate_content`` (a watcher
        content event) says the bytes may have changed anyway.
        ``recovered_at`` is set only by a completed recovery; it restarts the
        automatic cleanup retention window and holds nothing back.
        """
        ids = self._observe(
            vault_id,
            [(path, file_type, size, mtime_ns, observed_at)],
            seen_at=seen_at,
            recovered_at=recovered_at,
            invalidate_paths=[path] if invalidate_content else [],
        )
        return ids[path]

    def observe_local_copies_batch(
        self,
        *,
        vault_id: int,
        entries: Sequence[tuple[str, str, int | None, int | None, Any]],
        seen_at: Any | None = None,
        invalidate_paths: Sequence[str] = (),
    ) -> dict[str, str]:
        """Observe many Local Copies in a constant number of statements.

        Semantically identical to :meth:`observe_local_copy` per entry: one
        Vault lock touch, one bulk upsert, one consent revocation and one
        dirty-ancestor write. A repeated path keeps its last observation.
        Returns ``{path: file_id}``.
        """
        if not entries:
            return {}
        return self._observe(
            vault_id, entries, seen_at=seen_at, recovered_at=None,
            invalidate_paths=invalidate_paths,
        )

    def _observe(
        self,
        vault_id: int,
        entries: Sequence[tuple[str, str, int | None, int | None, Any]],
        *,
        seen_at: Any | None,
        recovered_at: Any | None,
        invalidate_paths: Sequence[str],
    ) -> dict[str, str]:
        last_by_path: dict[str, tuple[str, str, int | None, int | None, Any]] = {}
        for entry in entries:
            last_by_path[entry[0]] = entry
        ordered = list(last_by_path.values())
        paths = [entry[0] for entry in ordered]
        types = [entry[1] for entry in ordered]
        presences = ["present" if kind == "regular" else "unsupported" for kind in types]
        sizes = [entry[2] for entry in ordered]
        mtimes = [entry[3] for entry in ordered]
        observed = [_required_timestamp(entry[4]) for entry in ordered]
        seen = _timestamp(seen_at)
        seen_values = [seen or stamp for stamp in observed]
        recovered = _timestamp(recovered_at)
        same_stat = """
            f.local_presence = 'present'
            AND EXCLUDED.local_presence = 'present'
            AND f.local_size IS NOT DISTINCT FROM EXCLUDED.local_size
            AND f.local_mtime_ns IS NOT DISTINCT FROM EXCLUDED.local_mtime_ns
        """
        self._vault_lock_touch(vault_id)
        rows = self.connection.execute(
            f"""
            INSERT INTO files AS f (
                vault_id, path, path_since, local_presence, local_file_type,
                local_size, local_mtime_ns, local_seen_at, local_observed_at,
                local_content_observed_at, local_recovered_at
            )
            SELECT %s, s.path, s.observed_at, s.presence, s.file_type,
                   s.size, s.mtime_ns, s.seen_at, s.observed_at,
                   s.observed_at, %s::timestamptz
            FROM unnest(
                %s::text[], %s::text[], %s::text[], %s::bigint[], %s::bigint[],
                %s::timestamptz[], %s::timestamptz[]
            ) AS s(path, presence, file_type, size, mtime_ns, observed_at, seen_at)
            ON CONFLICT (vault_id, path) DO UPDATE SET
                generation = f.generation + 1,
                local_presence = EXCLUDED.local_presence,
                local_file_type = EXCLUDED.local_file_type,
                local_size = EXCLUDED.local_size,
                local_mtime_ns = EXCLUDED.local_mtime_ns,
                local_sha256 = CASE WHEN {same_stat} THEN f.local_sha256 END,
                matched_version_id = CASE WHEN {same_stat} THEN f.matched_version_id END,
                held_version_id = CASE
                    WHEN EXCLUDED.local_presence = 'present'
                     AND f.local_presence IS DISTINCT FROM 'present'
                    THEN CASE
                        WHEN EXCLUDED.local_recovered_at IS NULL
                        THEN {_only_cloud_copy_sql("f")}
                    END
                    ELSE f.held_version_id
                END,
                local_seen_at = EXCLUDED.local_seen_at,
                local_observed_at = EXCLUDED.local_observed_at,
                local_content_observed_at = CASE
                    WHEN (f.local_presence, f.local_file_type, f.local_size, f.local_mtime_ns)
                         IS DISTINCT FROM
                         (EXCLUDED.local_presence, EXCLUDED.local_file_type,
                          EXCLUDED.local_size, EXCLUDED.local_mtime_ns)
                    THEN EXCLUDED.local_observed_at
                    ELSE COALESCE(f.local_content_observed_at, EXCLUDED.local_observed_at)
                END,
                local_recovered_at = COALESCE(EXCLUDED.local_recovered_at, f.local_recovered_at)
            RETURNING f.path, f.id
            """,
            (
                vault_id, recovered,
                paths, presences, types, sizes, mtimes, observed, seen_values,
            ),
        ).fetchall()
        file_ids = {str(row["path"]): str(row["id"]) for row in rows}
        invalidated = sorted({path for path in invalidate_paths if path in file_ids})
        if invalidated:
            # A content event restarts the stability window and forgets the
            # fingerprint even when size and mtime were preserved.
            self.connection.execute(
                """
                UPDATE files
                SET local_sha256=NULL, matched_version_id=NULL,
                    local_content_observed_at=local_observed_at
                WHERE vault_id=%s AND path = ANY(%s::text[])
                  AND local_presence IS NOT NULL
                """,
                (vault_id, invalidated),
            )
        self._revoke_deletion_consents(
            vault_id, list(file_ids.values()), max(observed),
            reason="Cancelled: the Local Copy reappeared",
        )
        mark_paths_dirty(self.connection, vault_id, paths)
        return {path: file_ids[path] for path in paths}

    def mark_local_copy_missing(self, vault_file_id: str, *, observed_at: Any) -> None:
        """Record that the observed Local Copy is gone, keeping its fingerprint."""
        row = self.connection.execute(
            """
            UPDATE files
            SET local_presence='missing', local_observed_at=%s,
                generation=generation + 1
            WHERE id=%s AND local_presence IS NOT NULL
            RETURNING vault_id, path
            """,
            (_required_timestamp(observed_at), vault_file_id),
        ).fetchone()
        if row is not None:
            self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])

    def observe_replaced_local_copy(
        self,
        vault_file_id: str,
        *,
        file_type: str,
        size: int | None,
        mtime_ns: int | None,
        observed_at: Any,
    ) -> None:
        """Record content that replaced a Local Copy just freed or released.

        The previous content was just removed, so Current holds its only copy:
        a replacement observed in that race is held back like any content
        reappearing at a cloud-only path.
        """
        presence = "present" if file_type == "regular" else "unsupported"
        stamp = _required_timestamp(observed_at)
        row = self.connection.execute(
            f"""
            UPDATE files f
            SET generation=f.generation + 1,
                local_presence=%s, local_file_type=%s, local_size=%s,
                local_mtime_ns=%s, local_sha256=NULL, matched_version_id=NULL,
                held_version_id=CASE
                    WHEN CAST(%s AS text)='present' THEN {_only_cloud_copy_sql("f")}
                END,
                local_seen_at=%s, local_observed_at=%s,
                local_content_observed_at=%s
            WHERE f.id=%s
            RETURNING f.vault_id, f.path
            """,
            (presence, file_type, size, mtime_ns, presence, stamp, stamp, stamp, vault_file_id),
        ).fetchone()
        if row is not None:
            self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])

    def local_copies_to_fingerprint(self, vault_id: int) -> list[dict[str, Any]]:
        """Present regular Local Copies without a fingerprint, with their token.

        Hash each one outside any write transaction and publish it with
        :meth:`publish_local_fingerprint` using the ``generation`` read here.
        """
        rows = self.connection.execute(
            """
            SELECT id, path, generation, local_size, local_mtime_ns
            FROM files
            WHERE vault_id=%s
              AND local_presence='present'
              AND local_file_type='regular'
              AND local_sha256 IS NULL
            ORDER BY path
            """,
            (vault_id,),
        ).fetchall()
        return [
            {
                "vault_file_id": str(row["id"]),
                "path": row["path"],
                "generation": int(row["generation"]),
                "size": row["local_size"],
                "mtime_ns": row["local_mtime_ns"],
            }
            for row in rows
        ]

    def publish_local_fingerprint(
        self,
        vault_file_id: str,
        *,
        generation: int,
        plaintext_sha256: str | bytes,
        size: int | None,
        mtime_ns: int | None,
    ) -> bool:
        """Publish a digest for the observation it was computed from.

        Compare-and-swap on the generation read **before** hashing and on the
        stat the hash saw: a newer observation, a rename or a different stat
        wins and the digest is discarded (False). Content that reappeared at a
        cloud-only path and proves identical to Current becomes matched and
        is released from its hold; different content stays held.
        """
        digest = _digest(plaintext_sha256)
        settled = """CASE
            WHEN f.held_version_id IS NOT NULL THEN (
                SELECT held.id FROM archive_versions held
                WHERE held.id = f.held_version_id
                  AND held.integrity = 'verified'
                  AND held.availability = 'available'
                  AND held.plaintext_sha256 = %(digest)s
            )
            WHEN f.local_sha256 = %(digest)s THEN f.matched_version_id
        END"""
        row = self.connection.execute(
            f"""
            UPDATE files f
            SET local_sha256 = %(digest)s,
                matched_version_id = {settled},
                held_version_id = CASE
                    WHEN ({settled}) IS NOT NULL THEN NULL ELSE f.held_version_id
                END,
                pending_since = CASE
                    WHEN ({settled}) IS NOT NULL THEN NULL ELSE f.pending_since
                END
            WHERE f.id = %(id)s
              AND f.generation = %(generation)s
              AND f.local_presence = 'present'
              AND f.local_file_type = 'regular'
              AND f.local_size IS NOT DISTINCT FROM %(size)s::bigint
              AND f.local_mtime_ns IS NOT DISTINCT FROM %(mtime_ns)s::bigint
            RETURNING f.vault_id, f.path
            """,
            {
                "digest": digest,
                "id": vault_file_id,
                "generation": int(generation),
                "size": size,
                "mtime_ns": mtime_ns,
            },
        ).fetchone()
        if row is None:
            return False
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])
        return True

    def mark_unseen_local_copies_missing(
        self,
        *,
        vault_id: int,
        seen_at: Any,
        observed_at: Any,
        scan_started_at: Any | None = None,
    ) -> None:
        """Mark only rows not changed after this scan generation began.

        A watcher or another completed scan may legitimately update a Local
        Copy while the walk is in progress.  Such a newer observation must not
        be overwritten by the older scan's final missing transition.
        """
        started = _timestamp(scan_started_at)
        self.connection.execute(
            """
            UPDATE files
            SET local_presence='missing', local_observed_at=%s,
                generation=generation + 1
            WHERE vault_id=%s
              AND local_presence IN ('present', 'unsupported')
              AND local_seen_at IS DISTINCT FROM %s
              AND (%s::timestamptz IS NULL OR local_observed_at <= %s::timestamptz)
            """,
            (
                _required_timestamp(observed_at), vault_id,
                _required_timestamp(seen_at), started, started,
            ),
        )
        self._request_aggregate_rebuild(vault_id)

    def mark_local_path_missing(
        self, *, vault_id: int, path: str, observed_at: Any
    ) -> None:
        """Mark the Local Copy at ``path``, or every one below it, missing."""
        escaped = (
            path.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        self.connection.execute(
            """
            UPDATE files
            SET local_presence='missing', local_observed_at=%s,
                generation=generation + 1
            WHERE local_presence IS NOT NULL
              AND id IN (
                  SELECT id FROM files WHERE vault_id=%s AND path=%s
                  UNION ALL
                  SELECT id FROM files
                  WHERE vault_id=%s AND path LIKE %s ESCAPE '\\'
              )
            """,
            (
                _required_timestamp(observed_at),
                vault_id, path, vault_id, f"{escaped}/%",
            ),
        )
        # Path may be a file or a directory prefix of many descendants.
        self._mark_path_aggregates_dirty(vault_id, path)
        mark_directory_dirty(self.connection, vault_id, path)

    # -- explicit protection state ------------------------------------------

    def record_pending_files(
        self, vault_id: int, pending: Mapping[str, Any]
    ) -> dict[str, datetime]:
        """Keep one continuous pending timer per pending file of the Vault.

        ``pending`` maps each file that is pending now to the instant its
        pending state began. Existing timers never reset; files no longer
        pending lose theirs. Returns the durable ``{file_id: pending_since}``.
        """
        ids = list(pending)
        sinces = [_required_timestamp(pending[file_id]) for file_id in ids]
        rows = self.connection.execute(
            """
            UPDATE files f
            SET pending_since = COALESCE(f.pending_since, s.since)
            FROM unnest(%s::uuid[], %s::timestamptz[]) AS s(id, since)
            WHERE f.id = s.id AND f.vault_id = %s
            RETURNING f.id, f.pending_since
            """,
            (ids, sinces, vault_id),
        ).fetchall()
        self.connection.execute(
            """
            UPDATE files SET pending_since=NULL
            WHERE vault_id=%s AND pending_since IS NOT NULL
              AND NOT (id = ANY(%s::uuid[]))
            """,
            (vault_id, ids),
        )
        return {str(row["id"]): row["pending_since"] for row in rows}

    def block_reconciliation(self, vault_file_id: str, *, reason: str, at: Any) -> None:
        """Record why exact upload reconciliation of this file is blocked."""
        if not reason:
            raise ValueError("A reconciliation block needs a reason")
        self.connection.execute(
            """
            UPDATE files SET reconciliation_block=%s, reconciliation_blocked_at=%s
            WHERE id=%s
            """,
            (reason, _required_timestamp(at), vault_file_id),
        )

    def clear_reconciliation_block(self, vault_file_id: str) -> None:
        self.connection.execute(
            """
            UPDATE files SET reconciliation_block=NULL, reconciliation_blocked_at=NULL
            WHERE id=%s AND reconciliation_block IS NOT NULL
            """,
            (vault_file_id,),
        )

    # -- Current Snapshot and the replacement in progress --------------------

    def begin_replacement(
        self,
        vault_file_id: str,
        *,
        object_key: str,
        provider_version_id: str,
        size: int,
        storage_class: str | None = None,
        created_at: Any | None = None,
    ) -> str:
        """Record the uploaded object as this file's one replacement in progress.

        The Archive Version is born unverified and available at its exact
        provider identity; Current is unchanged until
        :meth:`promote_replacement`. Raises :class:`ReplacementInProgress` when
        another replacement is already in progress (nothing is written).
        """
        if not provider_version_id:
            raise ValueError("A provider VersionId is required")
        stamp = _timestamp(created_at) or datetime.now(timezone.utc)
        row = self.connection.execute(
            """
            WITH target AS (
                SELECT id FROM files
                WHERE id=%s AND replacing_version_id IS NULL
                FOR UPDATE
            ), version AS (
                INSERT INTO archive_versions(
                    file_id, object_key, provider_version_id, size, storage_class,
                    availability, availability_checked_at, created_at
                )
                SELECT target.id, %s, %s, %s, %s, 'available', %s, %s FROM target
                RETURNING id, file_id
            )
            UPDATE files f
            SET replacing_version_id = version.id,
                cloud_since = COALESCE(f.cloud_since, %s)
            FROM version
            WHERE f.id = version.file_id
            RETURNING version.id AS version_id, f.vault_id, f.path
            """,
            (
                vault_file_id, object_key, provider_version_id, size,
                storage_class, stamp, stamp, stamp,
            ),
        ).fetchone()
        if row is None:
            raise ReplacementInProgress("The Vault File already has a replacement in progress")
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])
        return str(row["version_id"])

    def promote_replacement(
        self,
        vault_file_id: str,
        *,
        archive_version_id: str,
        plaintext_sha256: str | bytes,
        verified_at: Any,
        local_generation: int | None = None,
    ) -> bool:
        """Verify the replacement in progress and make it Current, atomically.

        This is the only way Current changes. The superseded version stays
        unreferenced until its exact cleanup removes it (#591). The Local Copy
        becomes matched only when it is still the observation
        ``local_generation`` and its fingerprint equals the verified digest.
        Returns whether the Local Copy is matched to the new Current.
        """
        digest = _digest(plaintext_sha256)
        stamp = _required_timestamp(verified_at)
        target = self.connection.execute(
            "SELECT replacing_version_id FROM files WHERE id=%s FOR UPDATE",
            (vault_file_id,),
        ).fetchone()
        if target is None or _id(target["replacing_version_id"]) != str(archive_version_id):
            raise StaleReplacement("The Archive Version is not the replacement in progress")
        verified = self.connection.execute(
            """
            UPDATE archive_versions
            SET integrity='verified', verified_at=%s, plaintext_sha256=%s
            WHERE id=%s AND file_id=%s AND integrity='unverified'
              AND (plaintext_sha256 IS NULL OR plaintext_sha256=%s)
            RETURNING id
            """,
            (stamp, digest, archive_version_id, vault_file_id, digest),
        ).fetchone()
        if verified is None:
            raise StaleReplacement("The replacement cannot be verified with this digest")
        matches = """(
            %(generation)s::bigint IS NOT NULL
            AND f.generation = %(generation)s::bigint
            AND f.local_presence = 'present'
            AND f.local_sha256 = %(digest)s
        )"""
        row = self.connection.execute(
            f"""
            UPDATE files f
            SET current_version_id = f.replacing_version_id,
                replacing_version_id = NULL,
                held_version_id = NULL,
                matched_version_id = CASE WHEN {matches} THEN f.replacing_version_id END,
                pending_since = CASE WHEN {matches} THEN NULL ELSE f.pending_since END
            WHERE f.id = %(id)s AND f.replacing_version_id = %(version)s
            RETURNING f.vault_id, f.path, f.matched_version_id
            """,
            {
                "generation": local_generation,
                "digest": digest,
                "id": vault_file_id,
                "version": archive_version_id,
            },
        ).fetchone()
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])
        return row["matched_version_id"] is not None

    def abandon_replacement(
        self,
        vault_file_id: str,
        *,
        archive_version_id: str,
        mismatch_sha256: str | bytes | None = None,
        checked_at: Any | None = None,
    ) -> None:
        """End the replacement in progress without touching Current.

        With ``mismatch_sha256`` the version is recorded as a proven mismatch.
        The version stays as superseded until its exact cleanup (#591): a
        failed upload or verification always preserves the previous copy.
        """
        if mismatch_sha256 is not None:
            self.connection.execute(
                """
                UPDATE archive_versions
                SET integrity='mismatch', plaintext_sha256=%s, verified_at=%s
                WHERE id=%s AND file_id=%s AND integrity='unverified'
                """,
                (
                    _digest(mismatch_sha256), _required_timestamp(checked_at),
                    archive_version_id, vault_file_id,
                ),
            )
        row = self.connection.execute(
            """
            UPDATE files SET replacing_version_id=NULL
            WHERE id=%s AND replacing_version_id=%s
            RETURNING vault_id, path
            """,
            (vault_file_id, archive_version_id),
        ).fetchone()
        if row is None:
            raise StaleReplacement("The Archive Version is not the replacement in progress")
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])

    def mark_archive_versions_listed(self, version_ids: Sequence[str], *, scan_id: Any) -> None:
        """A complete listing saw these exact versions: they are available."""
        ids = [str(item) for item in version_ids if item]
        if not ids:
            return
        rows = self.connection.execute(
            """
            UPDATE archive_versions av
            SET availability='available', availability_checked_at=%s
            FROM files f
            WHERE av.id = ANY(%s::uuid[]) AND f.id = av.file_id
            RETURNING f.vault_id, f.path
            """,
            (_required_timestamp(scan_id), ids),
        ).fetchall()
        for row in rows:
            self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])

    def mark_unseen_archive_versions_missing(
        self, *, vault_id: int, scan_id: Any, scan_started_at: Any
    ) -> list[dict[str, Any]]:
        """Mark unseen versions missing and return the new transitions.

        Versions with a concurrent upload Job on their file, versions seen in
        this scan and versions created after it started are left unchanged so
        they cannot open false loss incidents.
        """
        terminal = list(TERMINAL_JOB_STATUSES)
        rows = self.connection.execute(
            """
            UPDATE archive_versions av
            SET availability='missing', availability_checked_at=%s
            FROM files f
            WHERE f.id = av.file_id
              AND f.vault_id = %s
              AND av.availability <> 'missing'
              AND av.availability_checked_at IS DISTINCT FROM %s
              AND av.created_at <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM jobs active_upload
                  WHERE active_upload.file_id = av.file_id
                    AND active_upload.action = 'upload'
                    AND NOT (active_upload.status = ANY(%s::text[]))
              )
            RETURNING av.id, f.vault_id, av.file_id, av.object_key,
                      av.provider_version_id, f.path
            """,
            (
                _required_timestamp(scan_id), vault_id, _required_timestamp(scan_id),
                _required_timestamp(scan_started_at), terminal,
            ),
        ).fetchall()
        # Bulk reconciliation can touch an unbounded set of paths.
        self._request_aggregate_rebuild(vault_id)
        return [
            {
                "id": str(row["id"]),
                "vault_id": int(row["vault_id"]),
                "vault_file_id": str(row["file_id"]),
                "object_key": row["object_key"],
                "provider_version_id": row["provider_version_id"],
                "path": row["path"],
            }
            for row in rows
        ]

    # -- lookups ------------------------------------------------------------

    def get_file(self, vault_file_id: str, *, vault_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            f"SELECT {_FILE_COLUMNS_SQL} FROM files f {_FILE_JOINS_SQL} "
            "WHERE f.id=%s AND f.vault_id=%s",
            (vault_file_id, vault_id),
        ).fetchone()
        return None if row is None else _file_dict(row)

    def get_file_by_path(self, vault_id: int, path: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            f"SELECT {_FILE_COLUMNS_SQL} FROM files f {_FILE_JOINS_SQL} "
            "WHERE f.vault_id=%s AND f.path=%s",
            (vault_id, path),
        ).fetchone()
        return None if row is None else _file_dict(row)

    def list_path_history(
        self, vault_file_id: str, *, vault_id: int
    ) -> list[dict[str, Any]]:
        """Every path of the file in order: closed mappings, then the current one."""
        rows = self.connection.execute(
            """
            SELECT path, valid_from, valid_to, 0 AS rank, id
            FROM file_path_history
            WHERE file_id=%s AND vault_id=%s
            UNION ALL
            SELECT path, path_since, NULL, 1, NULL
            FROM files
            WHERE id=%s AND vault_id=%s
            ORDER BY rank, valid_from, id
            """,
            (vault_file_id, vault_id, vault_file_id, vault_id),
        ).fetchall()
        return [
            {"path": row["path"], "valid_from": row["valid_from"], "valid_to": row["valid_to"]}
            for row in rows
        ]

    # -- renames ------------------------------------------------------------

    def rename_file(
        self,
        vault_file_id: str,
        *,
        new_path: str,
        changed_at: Any,
        vault_id: int,
    ) -> None:
        """Move one Vault File to ``new_path`` keeping identity and keeper.

        One transaction closes the old path in Path History, moves the row,
        invalidates the current observation, revokes pending deletion consent
        and dirties both ancestor chains. An occupied ``new_path`` fails on
        the unique path constraint and the caller's transaction rolls back.
        """
        stamp = _required_timestamp(changed_at)
        current = self.connection.execute(
            "SELECT path FROM files WHERE id=%s AND vault_id=%s FOR UPDATE",
            (vault_file_id, vault_id),
        ).fetchone()
        if current is None:
            raise VaultFileNotFound()
        if current["path"] == new_path:
            return
        self.connection.execute(
            """
            INSERT INTO file_path_history(file_id, vault_id, path, valid_from, valid_to)
            SELECT id, vault_id, path, path_since, %s FROM files
            WHERE id=%s AND vault_id=%s
            """,
            (stamp, vault_file_id, vault_id),
        )
        self.connection.execute(
            """
            UPDATE files
            SET path=%s, path_since=%s, generation=generation + 1
            WHERE id=%s AND vault_id=%s
            """,
            (new_path, stamp, vault_file_id, vault_id),
        )
        self._revoke_deletion_consents(
            vault_id, [vault_file_id], stamp, reason="Cancelled: the file was renamed",
        )
        mark_paths_dirty(self.connection, vault_id, [current["path"], new_path])

    def list_rename_candidates(
        self, vault_id: int, *, digest: str | bytes | None = None
    ) -> list[dict[str, Any]]:
        """Return only unique equal-digest matches within the scan window.

        Ambiguous, stale and changed-content paths remain independent Vault
        Files. The present side must be a brand-new identity: never one that
        has had cloud content or any Job. The missing side must have no open
        Job other than a deletion consent, which the rename revokes. A
        digest-scoped recheck uses the fingerprint index.
        """
        query = f"""
            SELECT
                f.id, f.path, f.local_presence, f.local_observed_at, f.local_sha256,
                (f.cloud_since IS NOT NULL
                 OR EXISTS (SELECT 1 FROM jobs j WHERE j.file_id = f.id)) AS has_history,
                EXISTS (
                    SELECT 1 FROM jobs j
                    WHERE j.file_id = f.id AND j.{_OPEN_JOB_SQL}
                      AND NOT (j.action = 'cloud-purge' AND j.status = 'pending_delay')
                ) AS has_open_job
            FROM files f
            WHERE f.vault_id=%s
              AND f.local_file_type='regular'
              AND f.local_sha256 IS NOT NULL
              AND f.local_presence IN ('missing', 'present')
              AND f.held_version_id IS NULL
        """
        params: list[Any] = [vault_id]
        if digest is not None:
            query += " AND f.local_sha256=%s"
            params.append(_digest(digest))
        query += " ORDER BY f.path"
        rows = self.connection.execute(query, tuple(params)).fetchall()
        missing_by_digest: dict[bytes, list[dict[str, Any]]] = {}
        present_by_digest: dict[bytes, list[dict[str, Any]]] = {}
        for row in rows:
            key = bytes(row["local_sha256"])
            if row["local_presence"] == "missing":
                missing_by_digest.setdefault(key, []).append(row)
            else:
                present_by_digest.setdefault(key, []).append(row)
        candidates: list[dict[str, Any]] = []
        for key, digest_missing in missing_by_digest.items():
            digest_present = present_by_digest.get(key, [])
            if len(digest_missing) != 1 or len(digest_present) != 1:
                continue
            missing, present = digest_missing[0], digest_present[0]
            if not _within_rename_window(
                missing["local_observed_at"], present["local_observed_at"]
            ):
                continue
            # A destination that has had cloud content or any Job is already an
            # independent file: never consume it or merge away its content.
            if present["has_history"] or missing["has_open_job"]:
                continue
            candidates.append(
                {
                    "missing_vault_file_id": str(missing["id"]),
                    "missing_path": missing["path"],
                    "new_vault_file_id": str(present["id"]),
                    "new_path": present["path"],
                    "digest": key.hex(),
                }
            )
        return candidates

    def _reserve_rename_confirmation(self, vault_id: int) -> None:
        """Serialize a confirmation before its candidate is inspected.

        The Vault row is locked before the two candidate rows, the same order
        scanner writes use, while the compare-and-swap writes below still fail
        closed for any writer that does not take it.
        """
        query = """
            SELECT id FROM vaults
            WHERE id=%s AND lifecycle='active'
            FOR NO KEY UPDATE
        """
        if self.connection.execute(query, (vault_id,)).fetchone() is None:
            raise VaultFileNotFound()

    def _require_one_rename_mutation(self, result: Any) -> None:
        """Fail closed when a conditional candidate mutation loses its CAS."""
        if getattr(result, "rowcount", None) != 1:
            self.connection.execute("ROLLBACK TO SAVEPOINT rename_confirmation_cas")
            self.connection.execute("RELEASE SAVEPOINT rename_confirmation_cas")
            raise VaultFileNotFound()

    def confirm_file_rename(
        self,
        *,
        vault_file_id: str,
        new_path: str,
        changed_at: Any,
        vault_id: int,
    ) -> str:
        """Atomically consume one current, Vault-local rename candidate.

        The missing Vault File keeps its identity and keeper (Current) and
        moves to ``new_path``; the provisional identity observed there, which
        never had cloud content or Jobs, is consumed. Every write is a
        compare-and-swap on the generations read under the row locks, so a
        competing confirmation or observation raises ``VaultFileNotFound`` and
        rolls back to the savepoint. Pending deletion consent is revoked and
        aggregates are invalidated in the same transaction.
        """
        stamp = _required_timestamp(changed_at)
        self._reserve_rename_confirmation(vault_id)
        pair = self.connection.execute(
            """
            SELECT
                missing.id AS missing_id, missing.path AS missing_path,
                missing.generation AS missing_generation,
                missing.local_sha256 AS missing_sha256,
                provisional.id AS provisional_id, provisional.path AS provisional_path,
                provisional.generation AS provisional_generation,
                provisional.local_sha256 AS provisional_sha256
            FROM files missing
            JOIN files provisional
              ON provisional.vault_id = missing.vault_id
             AND provisional.path = %s
             AND provisional.id <> missing.id
            WHERE missing.id = %s AND missing.vault_id = %s
            FOR UPDATE OF missing, provisional
            """,
            (new_path, vault_file_id, vault_id),
        ).fetchone()
        if (
            pair is None
            or pair["missing_sha256"] is None
            or pair["missing_sha256"] != pair["provisional_sha256"]
            or not any(
                candidate["missing_vault_file_id"] == str(vault_file_id)
                and candidate["new_path"] == new_path
                for candidate in self.list_rename_candidates(
                    vault_id, digest=bytes(pair["missing_sha256"])
                )
            )
        ):
            raise VaultFileNotFound()
        self.connection.execute("SAVEPOINT rename_confirmation_cas")
        consumed = self.connection.execute(
            """
            DELETE FROM files
            WHERE id=%s AND vault_id=%s AND generation=%s
              AND local_presence='present' AND cloud_since IS NULL
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id = files.id)
            RETURNING local_presence, local_file_type, local_size, local_mtime_ns,
                      local_sha256, local_seen_at, local_observed_at,
                      local_content_observed_at
            """,
            (pair["provisional_id"], vault_id, pair["provisional_generation"]),
        )
        local = consumed.fetchone() if consumed.rowcount == 1 else None
        self._require_one_rename_mutation(consumed)
        closed = self.connection.execute(
            """
            INSERT INTO file_path_history(file_id, vault_id, path, valid_from, valid_to)
            SELECT id, vault_id, path, path_since, %s FROM files
            WHERE id=%s AND vault_id=%s AND generation=%s AND path=%s
              AND local_presence='missing'
            """,
            (
                stamp, vault_file_id, vault_id, pair["missing_generation"],
                pair["missing_path"],
            ),
        )
        self._require_one_rename_mutation(closed)
        moved = self.connection.execute(
            """
            UPDATE files
            SET path=%s, path_since=%s, generation=generation + 1,
                local_presence=%s, local_file_type=%s, local_size=%s,
                local_mtime_ns=%s, local_sha256=%s, local_seen_at=%s,
                local_observed_at=%s, local_content_observed_at=%s,
                matched_version_id=NULL, held_version_id=NULL
            WHERE id=%s AND vault_id=%s AND generation=%s
            """,
            (
                new_path, stamp,
                local["local_presence"], local["local_file_type"], local["local_size"],
                local["local_mtime_ns"], local["local_sha256"], local["local_seen_at"],
                local["local_observed_at"], local["local_content_observed_at"],
                vault_file_id, vault_id, pair["missing_generation"],
            ),
        )
        self._require_one_rename_mutation(moved)
        self._revoke_deletion_consents(
            vault_id, [vault_file_id], stamp, reason="Cancelled: the file was renamed",
        )
        # Mark after CAS succeeds so a failed confirmation never dirties, and
        # still inside the savepoint so dirty rows share rename rollback.
        invalidate_for_confirmed_rename(
            self.connection,
            vault_id,
            old_path=pair["missing_path"],
            provisional_path=pair["provisional_path"],
            new_path=new_path,
        )
        self.connection.execute("RELEASE SAVEPOINT rename_confirmation_cas")
        return str(vault_file_id)

    def find_reusable_upload_version(
        self,
        *,
        vault_file_id: str,
        plaintext_sha256: str,
        bucket: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an existing Archive Version that already holds this digest.

        A verified available version is reused so unchanged Local Copy bytes do
        not create a duplicate S3 object. An unverified available version whose
        originating Job stored the same upload digest can resume verification
        without uploading again or deleting the prior version.
        """
        digest = plaintext_sha256.lower()
        verified = self.connection.execute(
            """
            SELECT
                id, object_key, provider_version_id, size AS cloud_size,
                plaintext_sha256 AS version_sha256, integrity, availability,
                storage_class, restore_state
            FROM archive_versions
            WHERE vault_file_id=%s
              AND integrity='verified'
              AND availability='available'
              AND plaintext_sha256=%s
              AND id=(SELECT current.id FROM archive_versions current WHERE current.vault_file_id=archive_versions.vault_file_id AND current.integrity='verified' AND current.availability='available' ORDER BY current.version_number DESC LIMIT 1)
              AND (restore_state IS NULL OR restore_state <> 'restoring')
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (vault_file_id, digest),
        ).fetchone()
        if verified is not None:
            from .services.snapshot_cleanup import _has_lineage
            exact = self.connection.execute("SELECT * FROM archive_versions WHERE id=%s", (verified["id"],)).fetchone()
            if _has_lineage(self.connection,exact,vault_file_id,bucket=bucket,provider=provider):
                return verified
        return self.connection.execute(
            """
            SELECT
                av.id, av.object_key, av.provider_version_id,
                av.size AS cloud_size, av.plaintext_sha256 AS version_sha256,
                av.integrity, av.availability, av.storage_class, av.restore_state,
                av.upload_snapshot_json, av.upload_verification_requested
            FROM archive_versions av
            WHERE av.vault_file_id=%s
              AND av.integrity='unverified'
              AND av.availability='available'
              AND (av.restore_state IS NULL OR av.restore_state <> 'restoring')
              AND EXISTS (
                  SELECT 1 FROM jobs prior
                  WHERE prior.archive_version_id=av.id AND prior.vault_file_id=av.vault_file_id
                    AND prior.vault_id=av.vault_id AND prior.action='upload'
                    AND prior.upload_plaintext_sha256=%s
              )
            ORDER BY av.version_number DESC
            LIMIT 1
            """,
            (vault_file_id, digest),
        ).fetchone()

    def link_job_version(self, job_id: int, archive_version_id: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET archive_version_id=%s WHERE id=%s",
            (archive_version_id, job_id),
        )

    def set_upload_plaintext_digest(
        self,
        job_id: int,
        *,
        plaintext_sha256: str,
    ) -> None:
        if len(plaintext_sha256) != 64:
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        int(plaintext_sha256, 16)
        self.connection.execute(
            """
            UPDATE jobs
            SET upload_plaintext_sha256=%s
            WHERE id=%s
            """,
            (plaintext_sha256.lower(), job_id),
        )

    def set_upload_snapshot(self, job_id: int, *, snapshot_json: str) -> None:
        from .services.upload_snapshot import UploadSnapshot
        snapshot = UploadSnapshot.from_json(snapshot_json)
        existing = self.connection.execute("SELECT upload_snapshot_json FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
        if existing and existing["upload_snapshot_json"] not in {None, snapshot_json}:
            raise RuntimeError("Upload Snapshot is immutable for an admitted intent")
        self.connection.execute("UPDATE jobs SET upload_snapshot_json=%s, upload_plaintext_sha256=%s WHERE id=%s",
                                (snapshot_json, snapshot.plaintext_sha256, job_id))

    def set_version_upload_snapshot(self, version_id: str, *, snapshot_json: str, requested: str) -> None:
        from .services.upload_snapshot import UploadSnapshot
        UploadSnapshot.from_json(snapshot_json)
        row = self.connection.execute("SELECT upload_snapshot_json, upload_verification_requested, integrity FROM archive_versions WHERE id=%s FOR UPDATE", (version_id,)).fetchone()
        if row is None:
            raise LookupError("Archive Version not found")
        if row["upload_snapshot_json"] is None and row["integrity"] == "verified":
            raise RuntimeError("Cannot reconstruct Upload Snapshot evidence for a verified Archive Version")
        if row and row["upload_snapshot_json"] is not None:
            if (row["upload_snapshot_json"], row["upload_verification_requested"]) != (snapshot_json, requested):
                raise RuntimeError("Archive Version Upload Snapshot is immutable")
            return
        self.connection.execute("UPDATE archive_versions SET upload_snapshot_json=%s, upload_verification_requested=%s WHERE id=%s",
                                (snapshot_json, requested, version_id))
        from .services.upload_remediation_discovery import capture
        capture(self.connection, datetime.now(timezone.utc).isoformat(), version_id=version_id)

    def set_version_verification_method(self, version_id: str, *, method: str, fallback_reason: str | None,
                                        egress_bytes: int | None, provider_proof: dict[str, Any] | None = None) -> None:
        # Attempts may update visible fallback while unverified. Once verified,
        # the effective method is historical evidence, not current Vault policy.
        self.connection.execute("UPDATE archive_versions SET verification_method=%s, verification_fallback_reason=%s, verification_egress_bytes=%s, provider_checksums=COALESCE(%s,provider_checksums) WHERE id=%s AND integrity<>'verified'",
                                (method, fallback_reason, egress_bytes, json.dumps(provider_proof) if provider_proof else None, version_id))

    def get_job_target(self, job_id: int) -> dict[str, Any] | None:
        return self.connection.execute(
            """
            SELECT
                j.id AS job_id,
                j.origin AS job_origin,
                j.vault_id,
                j.vault_file_id,
                j.archive_version_id,
                j.upload_plaintext_sha256,
                j.upload_snapshot_json AS job_upload_snapshot_json,
                j.upload_verification_requested AS job_upload_verification_requested,
                av.upload_snapshot_json,
                av.upload_verification_requested,
                av.verification_method,
                av.verification_fallback_reason,
                av.verification_egress_bytes,
                j.path,
                lc.presence AS local_presence,
                lc.file_type AS local_file_type,
                lc.size AS local_size,
                lc.mtime_ns AS local_mtime_ns,
                lc.recovered_at AS local_recovered_at,
                av.verified_at,
                (SELECT latest.id FROM archive_versions latest
                 WHERE latest.vault_file_id=j.vault_file_id AND latest.availability<>'purged'
                 ORDER BY latest.version_number DESC LIMIT 1) AS latest_archive_version_id,
                COALESCE(lc.content_observed_at, lc.observed_at) AS content_observed_at,
                lc.plaintext_sha256 AS local_sha256,
                lc.matched_archive_version_id,
                lc.cloud_only_version_id,
                av.object_key,
                av.provider_version_id,
                av.size AS cloud_size,
                av.storage_class,
                av.plaintext_sha256 AS version_sha256,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry
            FROM jobs j
            LEFT JOIN local_copies lc ON lc.vault_file_id=j.vault_file_id
            LEFT JOIN archive_versions av ON av.id=j.archive_version_id
            WHERE j.id=%s
            """,
            (job_id,),
        ).fetchone()

    def update_restore_state(
        self,
        archive_version_id: str,
        *,
        state: str | None,
        expiry: str | None,
        checked_at: str,
        storage_class: str | None = None,
    ) -> None:
        owned = self.connection.execute(
            """
            SELECT vault_id, vault_file_id
            FROM archive_versions
            WHERE id=%s
            """,
            (archive_version_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE archive_versions
            SET restore_state=%s,
                restore_expiry=%s,
                restore_checked_at=%s,
                storage_class=COALESCE(%s, storage_class)
            WHERE id=%s
            """,
            (state, expiry, checked_at, storage_class, archive_version_id),
        )
        if owned is not None:
            self._mark_file_aggregates_dirty(
                int(owned["vault_id"]), owned["vault_file_id"]
            )

    def update_version_storage_placement(
        self,
        archive_version_id: str,
        *,
        provider_version_id: str,
        storage_class: str,
        etag: str | None = None,
        observed_at: str,
        storage_class_source: str = "manual",
    ) -> None:
        """Preserve Archive Version identity while recording a class/placement change."""
        owned = self.connection.execute(
            """
            SELECT vault_id, vault_file_id
            FROM archive_versions
            WHERE id=%s
            """,
            (archive_version_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE archive_versions
            SET provider_version_id=%s,
                storage_class=%s,
                etag=COALESCE(%s, etag),
                storage_class_source=%s,
                availability='available',
                availability_checked_at=%s,
                restore_state=NULL,
                restore_expiry=NULL,
                restore_checked_at=%s
            WHERE id=%s
            """,
            (
                provider_version_id,
                storage_class,
                etag,
                storage_class_source,
                observed_at,
                observed_at,
                archive_version_id,
            ),
        )
        if owned is not None:
            self._mark_file_aggregates_dirty(
                int(owned["vault_id"]), owned["vault_file_id"]
            )

    def publish_storage_class_copy(
        self,
        *,
        job_id: int,
        archive_version_id: str,
        provider_version_id: str,
        storage_class: str,
        etag: str | None,
        observed_at: str,
    ) -> str:
        """Publish a verified placement while keeping one Archive Version identity.

        S3 retains the prior exact VersionId as a noncurrent provider version;
        the catalog continues to represent the logical Archive Version with one
        row, as required by the manual storage-class Job contract.  A later
        cloud scan can rediscover the retained provider version, and the
        dedicated Cloud Purge workflow remains the only permanent deletion
        path.
        """
        if not provider_version_id:
            raise ValueError("A provider VersionId is required")
        source = self.connection.execute(
            """
            SELECT vault_file_id, provider_version_id
            FROM archive_versions
            WHERE id=%s
            """,
            (archive_version_id,),
        ).fetchone()
        if source is None:
            raise LookupError("Archive Version is no longer available")
        self.update_version_storage_placement(
            archive_version_id,
            provider_version_id=str(provider_version_id),
            storage_class=storage_class,
            etag=etag,
            observed_at=observed_at,
        )
        self.connection.execute(
            "UPDATE jobs SET archive_version_id=%s WHERE id=%s",
            (archive_version_id, job_id),
        )
        return archive_version_id

    def list_versions(self, vault_id: int, path: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT
                av.id,
                av.version_number,
                av.object_key,
                av.provider_version_id,
                av.size,
                av.storage_class,
                av.storage_class_source,
                av.origin,
                av.etag,
                av.plaintext_sha256,
                av.uploaded_at,
                av.verified_at,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry,
                av.upload_verification_requested, av.verification_method,
                av.verification_fallback_reason, av.verification_egress_bytes
            FROM archive_versions av
            JOIN vault_files vf ON vf.id=av.vault_file_id
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE vf.vault_id=%s AND fp.vault_id=vf.vault_id AND fp.path=%s
            ORDER BY av.version_number DESC
            """,
            (vault_id, path),
        ).fetchall()
        return [
            {
                **row,
                "recoverable": (
                    row["integrity"] == "verified"
                    and row["availability"] == "available"
                ),
                "not_selectable_reason": (
                    None
                    if row["integrity"] == "verified"
                    and row["availability"] == "available"
                    else (
                        row["availability"]
                        if row["availability"] in {"missing", "purged", "unknown"}
                        else row["integrity"]
                    )
                ),
            }
            for row in rows
        ]

    def list_file_rows(
        self,
        vault_id: int,
        *,
        search: str = "",
        path_prefix: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["vf.vault_id=%s", "fp.vault_id=vf.vault_id", "vf.status='active'"]
        params: list[Any] = [vault_id]
        if search:
            clauses.append("lower(fp.path) LIKE lower(%s)")
            params.append(f"%{search}%")
        if path_prefix:
            escaped = (
                path_prefix.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("fp.path LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}/%")
        clauses.append(VISIBLE_SQL)
        rows = self.connection.execute(
            self._file_row_select_sql(clauses, vault_id) + " ORDER BY lower(fp.path)", params,
        ).fetchall()
        return self._materialize_file_items(vault_id, rows, name_from_path=True)

    def list_files_page(
        self,
        vault_id: int,
        *,
        search: str = "",
        directory: str = "",
        state: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Return one page of browse/search items without full-catalog materialization.

        Browse mode reads durable directory aggregates for child folders and
        pages direct child files in SQL. Search mode filters and pages Vault
        File rows in SQL. Both paths keep ``last_listing_rows_materialized``
        equal to the returned page cardinality (not the descendant total).
        """
        aggregate_status = ensure_directory_aggregates(self.connection, vault_id)
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        if search:
            items, total = self._search_files_page(
                vault_id,
                search=search,
                state=state,
                page=page,
                page_size=page_size,
            )
            mode = "search"
        else:
            items, total = self._browse_directory_page(
                vault_id,
                directory=directory or "",
                state=state,
                page=page,
                page_size=page_size,
            )
            mode = "browse"
        self.last_listing_rows_materialized = len(items)
        return {
            "items": items,
            "total": total,
            "page": page,
            "directory": directory or "",
            "mode": mode,
            # ready | loading | stale — listing never blocks on full rebuild.
            "aggregate_status": aggregate_status,
        }

    def search_protected_vaults(
        self, vaults: list[dict[str, Any]], *, search: str, state: str,
        page: int, page_size: int,
    ) -> dict[str, Any]:
        """SQL-bounded cross-Vault search; never change Session selection.

        Each branch uses that Vault's current source observation. Filtering before
        LIMIT and COUNT prevents false Cloud-only matches on unavailable mounts.
        Only the requested page is materialized, including duplicate paths.
        """
        branches, params = [], []
        for vault in vaults:
            catalog = ArchiveCatalog(self.connection, protection_unconfirmed=vault["unconfirmed"])
            clauses, values = catalog._direct_child_file_clauses(
                vault["id"], directory="", state=state, search=search or "%",
            )
            sql = catalog._file_row_select_sql(clauses, vault["id"])
            branches.append(f"SELECT scoped.*, %s::bigint AS vault_id, %s::text AS vault_name FROM ({sql}) scoped")
            params.extend([vault["id"], vault["name"], *values])
        if not branches:
            return {"items": [], "total": 0, "page": page, "mode": "search", "directory": "", "aggregate_status": "ready"}
        union = " UNION ALL ".join(branches)
        total = self.connection.execute(f"SELECT count(*) AS total FROM ({union}) matches", params).fetchone()["total"]
        rows = self.connection.execute(
            f"SELECT * FROM ({union}) matches ORDER BY lower(path), path, vault_id LIMIT %s OFFSET %s",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        by_id = {vault["id"]: vault for vault in vaults}
        items = []
        for row in rows:
            vault = by_id[row["vault_id"]]
            catalog = ArchiveCatalog(self.connection, protection_unconfirmed=vault["unconfirmed"])
            for item in catalog._materialize_file_items(vault["id"], [row], name_from_path=True):
                items.append({**item, "vault_id": vault["id"], "vault_name": vault["name"]})
        return {"items": items, "total": total, "page": page, "mode": "search", "directory": "", "aggregate_status": "ready"}

    def _browse_directory_page(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        parent_path = directory or ""
        folder_state = state
        if self.protection_unconfirmed:
            folder_state = "" if not state or state == "unconfirmed" else "__none__"
        folder_total = count_child_directories(
            self.connection,
            vault_id,
            parent_path=parent_path,
            state_filter=folder_state,
        )
        file_total = self._count_direct_child_files(
            vault_id,
            directory=parent_path,
            state=state,
        )
        total = folder_total + file_total
        offset = (page - 1) * page_size
        items: list[dict[str, Any]] = []
        if offset < folder_total:
            folders = list_child_directory_rows(
                self.connection,
                vault_id,
                parent_path=parent_path,
                state_filter=folder_state,
                limit=page_size,
                offset=offset,
                unconfirmed=self.protection_unconfirmed,
            )
            items.extend(folders)
            remaining = page_size - len(items)
            if remaining > 0:
                items.extend(
                    self._list_direct_child_files(
                        vault_id,
                        directory=parent_path,
                        state=state,
                        limit=remaining,
                        offset=0,
                    )
                )
        else:
            file_offset = offset - folder_total
            items.extend(
                self._list_direct_child_files(
                    vault_id,
                    directory=parent_path,
                    state=state,
                    limit=page_size,
                    offset=file_offset,
                )
            )
        return items, total

    def _direct_child_file_clauses(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
        search: str = "",
    ) -> tuple[list[str], list[Any]]:
        clauses = ["vf.vault_id=%s", "fp.vault_id=vf.vault_id", "vf.status='active'"]
        params: list[Any] = [vault_id]
        if search:
            clauses.append("lower(fp.path) LIKE lower(%s)")
            params.append(f"%{search}%")
        elif directory:
            escaped = (
                directory.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            # Immediate children only.
            clauses.append("fp.path LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}/%")
            clauses.append("fp.path NOT LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}/%/%")
        else:
            clauses.append("fp.path NOT LIKE %s")
            params.append("%/%")
        clauses.append(VISIBLE_SQL)
        if state:
            if self.protection_unconfirmed:
                clauses.append("1=1" if state == "unconfirmed" else "1=0")
            else:
                clauses.append(f"({self._state_sql(vault_id)}) = %s")
                params.append(state)
        return clauses, params

    def _state_sql(self, vault_id: int) -> str:
        """The protection CASE with this Vault's upload exclusions embedded."""
        from .services.operation_policies import get_policy
        return state_sql(get_policy(self.connection, vault_id))

    def _path_excluded_sql(self, vault_id: int) -> str:
        from .services.operation_policies import get_policy
        return path_excluded_sql(get_policy(self.connection, vault_id))

    def _file_row_select_sql(self, clauses: list[str], vault_id: int) -> str:
        return f"""
            SELECT
                {self._state_sql(vault_id)} AS protection_state,
                {self._path_excluded_sql(vault_id)} AS path_excluded,
                CASE WHEN av.availability='unknown'
                     THEN greatest(lc.observed_at, av.verified_at)
                     ELSE greatest(lc.observed_at, av.verified_at, av.availability_checked_at)
                END AS protection_observed_at,
                fp.path,
                lc.presence AS local_presence,
                lc.file_type AS local_file_type,
                lc.size AS local_size,
                lc.mtime_ns AS local_mtime_ns,
                lc.plaintext_sha256 AS local_sha256,
                lc.matched_archive_version_id,
                lc.cloud_only_version_id,
                av.id AS archive_version_id,
                av.size AS cloud_size,
                av.storage_class,
                av.plaintext_sha256 AS version_sha256,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry,
                av.upload_verification_requested, av.verification_method,
                av.verification_fallback_reason, av.verification_egress_bytes,
                (
                    SELECT COUNT(*)
                    FROM archive_versions recoverable
                    WHERE recoverable.vault_file_id=vf.id
                      AND recoverable.integrity='verified'
                      AND recoverable.availability='available'
                ) AS recoverable_version_count
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
            {LATEST_JOIN_SQL}
            WHERE {" AND ".join(clauses)}
        """

    def _materialize_file_items(
        self,
        vault_id: int,
        rows: list[dict[str, Any]],
        *,
        name_from_path: bool,
        directory: str = "",
    ) -> list[dict[str, Any]]:
        prefix = f"{directory}/" if directory else ""
        items: list[dict[str, Any]] = []
        for row in rows:
            local_exists = row["local_presence"] in {"present", "unsupported"}
            cloud_exists = row["archive_version_id"] is not None and row["availability"] not in {"missing", "purged"}
            if not local_exists and row["archive_version_id"] is None:
                continue
            protection = project_file(row, unconfirmed=self.protection_unconfirmed)
            upload_eligible = local_copy_is_upload_eligible(row)
            recoverable_count = int(row["recoverable_version_count"] or 0)
            recover_eligible = not local_exists and recoverable_count > 0
            cleanup_eligible = (
                row["local_presence"] == "present"
                and row["local_file_type"] == "regular"
                and cloud_exists
                and row["integrity"] == "verified"
                and row["availability"] == "available"
                and row["matched_archive_version_id"] == row["archive_version_id"]
                and row["local_sha256"] is not None
                and row["local_sha256"] == row["version_sha256"]
            )
            if name_from_path:
                name = row["path"]
            else:
                name = row["path"][len(prefix):] if prefix else row["path"]
            items.append(
                {
                    "type": "file",
                    "name": name,
                    "path": row["path"],
                    "local_exists": int(local_exists),
                    "local_size": row["local_size"],
                    "modified_at": (datetime.fromtimestamp(int(row["local_mtime_ns"]) / 1e9, timezone.utc).isoformat()
                                    if row.get("local_mtime_ns") else None),
                    "local_file_type": row["local_file_type"],
                    "cloud_exists": int(cloud_exists),
                    "cloud_size": row["cloud_size"],
                    "storage_class": row["storage_class"],
                    "integrity": row["integrity"],
                    "availability": row["availability"],
                    "restore_state": row["restore_state"],
                    "restore_expiry": row["restore_expiry"],
                    "upload_verification_requested": row.get("upload_verification_requested"),
                    "verification_method": row.get("verification_method"),
                    "verification_fallback_reason": row.get("verification_fallback_reason"),
                    "verification_egress_bytes": row.get("verification_egress_bytes"),
                    **protection,
                    "cloud_only_conflict": bool(row.get("cloud_only_version_id")),
                    "upload_eligible": upload_eligible and not self.protection_unconfirmed,
                    "recover_eligible": recover_eligible,
                    "recoverable_version_count": recoverable_count,
                    "cleanup_eligible": cleanup_eligible and protection["state"] == "protected",
                }
            )
        return items

    def _count_direct_child_files(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
    ) -> int:
        clauses, params = self._direct_child_file_clauses(
            vault_id, directory=directory, state=state
        )
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
            {LATEST_JOIN_SQL}
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchone()
        return int(row["total"] or 0) if row else 0

    def _list_direct_child_files(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        clauses, params = self._direct_child_file_clauses(
            vault_id, directory=directory, state=state
        )
        sql = self._file_row_select_sql(clauses, vault_id)
        rows = self.connection.execute(
            f"""
            {sql}
            ORDER BY lower(fp.path) ASC, fp.path ASC
            LIMIT %s OFFSET %s
            """,
            [*params, int(limit), max(0, int(offset))],
        ).fetchall()
        return self._materialize_file_items(
            vault_id,
            rows,
            name_from_path=False,
            directory=directory,
        )

    def _search_files_page(
        self,
        vault_id: int,
        *,
        search: str,
        state: str,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses, params = self._direct_child_file_clauses(
            vault_id,
            directory="",
            state=state,
            search=search,
        )
        # The shared clauses already apply search, visibility and protection.

        total_row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
            {LATEST_JOIN_SQL}
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchone()
        total = int(total_row["total"] or 0) if total_row else 0
        offset = (page - 1) * page_size
        sql = self._file_row_select_sql(clauses, vault_id)
        rows = self.connection.execute(
            f"""
            {sql}
            ORDER BY lower(fp.path) ASC, fp.path ASC
            LIMIT %s OFFSET %s
            """,
            [*params, int(page_size), max(0, int(offset))],
        ).fetchall()
        items = self._materialize_file_items(
            vault_id,
            rows,
            name_from_path=True,
        )
        return items, total

    @staticmethod
    def _claimable_job_status_sql(
        *,
        prefix: str,
        now: str,
        restore_due_before: str,
        include_restoring: bool = True,
    ) -> tuple[str, list[Any]]:
        """Return the durable scheduler-state predicate shared by claim/count.

        A claim only starts work from an explicitly runnable state.  Intermediate
        states (``uploading``, ``cleaning``, …) are deliberately absent: a stale
        lease in one of those states must first pass restart reconciliation rather
        than be run as though it were a fresh queued Job.
        """
        status = f"{prefix}status"
        action = f"{prefix}action"
        retry_after = f"{prefix}retry_after"
        pending_until = f"{prefix}pending_until"
        updated_at = f"{prefix}updated_at"
        clauses = [
            f"{status}='queued'",
            f"({status}='retrying' AND ({retry_after} IS NULL OR {retry_after} <= %s))",
            (
                f"({action}='cloud-purge' AND {status}='pending_delay' "
                f"AND {pending_until} IS NOT NULL AND {pending_until} <= %s)"
            ),
        ]
        params: list[Any] = [now, now]
        if include_restoring:
            clauses.append(
                f"({action} IN ('recover', 'storage-class') "
                f"AND {status}='restoring' AND {updated_at} <= %s)"
            )
            params.append(restore_due_before)
        return "(" + " OR ".join(clauses) + ")", params

    @classmethod
    def _claimable_job_predicate_sql(
        cls,
        *,
        prefix: str,
        now: str,
        restore_due_before: str,
    ) -> tuple[str, list[Any]]:
        """Build the one durable claimability predicate for scheduler/metrics.

        Keeping candidate selection, conditional acquisition, and queue depth on
        this predicate prevents monitoring from reporting rows a worker cannot
        acquire.  In particular, due ``restoring`` Jobs and expired leases use
        exactly the same definition everywhere.
        """
        runnable_sql, params = cls._claimable_job_status_sql(
            prefix=prefix,
            now=now,
            restore_due_before=restore_due_before,
            include_restoring=True,
        )
        vault_id = f"{prefix}vault_id"
        group_id = f"{prefix}group_id"
        action = f"{prefix}action"
        lease_token = f"{prefix}claim_token"
        lease_expiry = f"{prefix}claim_expires_at"
        correlated_prefix = prefix or "jobs."
        predicate = f"""
            {runnable_sql}
            AND (
                    {action} = 'recover'
                 OR ({action} = 'storage-class' AND {prefix}status = 'restoring')
                 OR {prefix}origin = 'stop-protection'
                 OR NOT COALESCE((
                        SELECT pause_vault.paused FROM vaults pause_vault
                        WHERE pause_vault.id={correlated_prefix}vault_id
                    ), FALSE)
            )
            AND (
                    {prefix}action <> 'upload'
                 OR {prefix}origin <> 'automatic'
                 OR {prefix}archive_version_id IS NOT NULL
                 OR COALESCE((
                        SELECT upload_policy.auto_upload
                        FROM vault_operation_policies upload_policy
                        WHERE upload_policy.vault_id={correlated_prefix}vault_id
                    ), TRUE)
            )
            AND (
                    {lease_token} IS NULL
                 OR {lease_expiry} IS NULL
                 OR {lease_expiry} <= %s
            )
            AND (
                    {action} <> 'cloud-purge'
                 OR (
                        {group_id} IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs purge_peer
                        WHERE purge_peer.vault_id={vault_id}
                          AND purge_peer.group_id={group_id}
                          AND purge_peer.action='cloud-purge'
                          AND purge_peer.status NOT IN ('completed', 'failed', 'cancelled')
                          AND (
                                (
                                    purge_peer.status <> 'queued'
                                AND NOT (
                                    purge_peer.status='pending_delay'
                                    AND purge_peer.pending_until IS NOT NULL
                                    AND purge_peer.pending_until <= %s
                                )
                                )
                            OR (
                                    purge_peer.claim_token IS NOT NULL
                                AND purge_peer.claim_expires_at IS NOT NULL
                                AND purge_peer.claim_expires_at > %s
                            )
                          )
                    )
                 )
            )
        """
        return predicate, [*params, now, now, now]

    # SQL twin of ``job_scheduler.job_priority`` (alias ``j``): 0 Owner
    # operations, 1 uploads the Owner asked for, 2 anything admitted automatically.
    _SCHEDULING_TIER_SQL = (
        "CASE WHEN j.origin='automatic' THEN 2 WHEN j.action<>'upload' THEN 0 ELSE 1 END"
    )

    def list_claimable_jobs(
        self,
        *,
        now: str,
        restore_due_before: str,
        limit: int,
        per_vault_limit: int | None = None,
        exclude_job_ids: Sequence[int] = (),
        unserved_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Load scheduler candidates using the shared claimability definition.

        The window is taken **per Vault and per scheduling tier**
        (``per_vault_limit``, default ``limit``), never over the whole
        installation: a Vault holding thousands of older Jobs, or Jobs the worker
        cannot run, must not push every other Vault's work (or its own uploads
        behind a flood of Owner operations) out of the candidate set (issues
        #493, #500). Tiers match ``job_scheduler.job_priority``. ``exclude_job_ids`` removes candidates
        the worker already found not runnable *before* the window is cut.
        ``limit`` defaults the per-Vault window, not a global cut: every eligible
        Vault must be visible to the turn selector, including aged tier-2 work.
        """
        if limit <= 0:
            return []
        window = int(per_vault_limit) if per_vault_limit else int(limit)
        predicate, params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        excluded = [int(job_id) for job_id in exclude_job_ids]
        tier = self._SCHEDULING_TIER_SQL
        # When no slot is free we only need starvation observations, not the
        # already-served bulk queue's windows or local readiness checks.
        idle_filter = """
            AND NOT EXISTS (
                SELECT 1 FROM jobs serving WHERE serving.vault_id=v.id
                  AND serving.claim_token IS NOT NULL AND serving.claim_expires_at > %s
                  AND serving.status NOT IN ('completed', 'failed', 'cancelled')
            )
        """ if unserved_only else ""
        return self.connection.execute(
            f"""
            SELECT j.*, v.source_root, v.s3_bucket,
                   v.s3_prefix,
                   v.uuid AS vault_uuid, v.name AS vault_name,
                   v.decommission_state
            FROM vaults v
            CROSS JOIN LATERAL (
                SELECT ranked.* FROM (
                    SELECT j.*,
                           row_number() OVER (
                               PARTITION BY {tier}
                               ORDER BY j.requested_at ASC, j.id ASC
                           ) AS tier_rank
                    FROM jobs j
                    WHERE j.vault_id=v.id
                      AND (
                            (v.enabled=TRUE AND v.decommission_state='active'
                             AND j.origin IN ('manual','automatic'))
                         OR (v.decommission_state='decommissioning'
                             AND j.origin='stop-protection' AND j.action='cloud-purge')
                      )
                      AND ({predicate})
                      AND j.id <> ALL(%s::bigint[])
                ) ranked
                WHERE ranked.tier_rank <= {window}
            ) j
            WHERE v.relocation_state='ready' {idle_filter}
            ORDER BY {tier} ASC,
                     j.requested_at ASC, j.id ASC
            """,
            [*params, excluded, *([now] if unserved_only else [])],
        ).fetchall()

    def observe_scheduler_turns(
        self, *, now: str, runnable_vault_ids: Sequence[int],
    ) -> dict[int, dict[str, Any]]:
        """Bank only observed runnable/unserved time, durably across restarts.

        The worker validates one runnable head per Vault (including the local
        automatic-upload fence) before observing. A live lease pauses aging.
        SQL state changes invalidate open intervals even between observations;
        missing/non-runnable Vaults retain banked credit but earn nothing new.
        SKIP LOCKED avoids blocking claims; a skipped turn gets no promotion.
        """
        runnable = sorted({int(vault_id) for vault_id in runnable_vault_ids})
        rows = self.connection.execute(
            "SELECT * FROM scheduler_vault_turns ORDER BY vault_id FOR UPDATE SKIP LOCKED"
        ).fetchall()
        active = self.active_job_counts(now=now)
        result = {}
        for row in rows:
            vault_id = int(row["vault_id"])
            eligible = vault_id in runnable and not active.get(vault_id, 0)
            updated = self.connection.execute(
                """UPDATE scheduler_vault_turns
                   SET wait_seconds=wait_seconds + CASE
                         WHEN %s AND eligible_since IS NOT NULL
                         THEN GREATEST(0, EXTRACT(EPOCH FROM (%s::timestamptz-eligible_since)))
                         ELSE 0 END,
                       eligible_since=CASE WHEN %s THEN
                         GREATEST(eligible_since, %s::timestamptz) ELSE NULL END
                   WHERE vault_id=%s
                   RETURNING wait_seconds AS scheduler_wait_seconds,
                             last_served_at::text AS scheduler_last_served_at,
                             generation AS scheduler_generation""",
                (eligible, now, eligible, now, vault_id),
            ).fetchone()
            if vault_id in runnable:
                result[vault_id] = dict(updated)
        return result

    def active_job_counts(self, *, now: str) -> dict[int, int]:
        """Jobs currently holding a live lease, per Vault (scheduler fairness)."""
        rows = self.connection.execute(
            """
            SELECT vault_id, count(*) AS total FROM jobs
            WHERE claim_token IS NOT NULL AND claim_expires_at > %s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            GROUP BY vault_id
            """,
            (now,),
        ).fetchall()
        return {int(row["vault_id"]): int(row["total"]) for row in rows}

    @staticmethod
    def _claim_is_available(row: dict[str, Any], *, now: str) -> bool:
        token = row.get("claim_token")
        expiry = row.get("claim_expires_at")
        # A partially written legacy/malformed claim never blocks recovery.
        return not token or not expiry or str(expiry) <= now

    @staticmethod
    def _purge_job_is_runnable(row: dict[str, Any], *, now: str) -> bool:
        status = row.get("status")
        if status == "queued":
            return True
        return (
            status == "pending_delay"
            and row.get("pending_until") is not None
            and str(row["pending_until"]) <= now
        )

    def _lock_claim_vault(self, job_id: int) -> bool:
        """Vault-first shared fence against pause/lifecycle changes.

        Skip a contended Vault instead of blocking the scheduler. The following
        statement sees pause's committed flag, even after stale candidate reads.
        Shared claims and held progress steps remain concurrent (ADR-0031).
        """
        return self.connection.execute(
            """SELECT v.id FROM vaults v
               WHERE v.id=(SELECT vault_id FROM jobs WHERE id=%s)
               FOR KEY SHARE SKIP LOCKED""", (job_id,),
        ).fetchone() is not None

    def _lock_scheduler_turn(self, job_id: int, generation: int | None) -> bool:
        """Lock accounting before acquiring any Job locks.

        Base claims can briefly queue here; aged nominations skip contention.
        Once holding the turn row, callers MUST use SKIP LOCKED on Jobs: a bulk
        status writer can already hold Jobs and need this row to invalidate an
        interval. Never wait for its Jobs, nor hold a Job while waiting here.
        Ordinary progress retains ADR-0031 shared Vault fences.
        """
        job = self.connection.execute(
            "SELECT vault_id FROM jobs WHERE id=%s", (job_id,),
        ).fetchone()
        if job is None:
            return False
        lock = "FOR UPDATE SKIP LOCKED" if generation is not None else "FOR UPDATE"
        turn = self.connection.execute(
            "SELECT generation FROM scheduler_vault_turns WHERE vault_id=%s " + lock,
            (job["vault_id"],),
        ).fetchone()
        return turn is not None and (generation is None or turn["generation"] == generation)

    def _consume_scheduler_turn(self, job_id: int, *, now: str) -> None:
        self.connection.execute(
            """UPDATE scheduler_vault_turns
               SET wait_seconds=0, eligible_since=NULL,
                   last_served_at=GREATEST(last_served_at, %s::timestamptz),
                   generation=generation+1
               WHERE vault_id=(SELECT vault_id FROM jobs WHERE id=%s)""", (now, job_id),
        )

    def claim_job(
        self,
        *,
        job_id: int,
        claim_token: str,
        claimed_at: str,
        claim_expires_at: str,
        now: str,
        restore_due_before: str,
        scheduler_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically lease one runnable Job.

        The conditional UPDATE locks the matching PostgreSQL row while it
        rechecks the predicate.  A loser therefore observes no
        returned row instead of executing a stale in-memory selection.
        """
        if not claim_token:
            raise ValueError("A Job claim token is required")
        if not self._lock_claim_vault(job_id):
            return None
        if not self._lock_scheduler_turn(job_id, scheduler_generation):
            return None
        if self.connection.execute(
            'SELECT id FROM jobs WHERE id=%s FOR UPDATE SKIP LOCKED', (job_id,),
        ).fetchone() is None:
            return None
        claimable_sql, claimable_params = self._claimable_job_predicate_sql(
            prefix="",
            now=now,
            restore_due_before=restore_due_before,
        )
        claimed = self.connection.execute(
            f"""
            UPDATE jobs
            SET claim_token=%s,
                claimed_at=%s,
                claim_expires_at=%s
            WHERE id=%s
              AND action<>'cloud-purge'
              AND ({claimable_sql})
            RETURNING id, status, claim_token, claimed_at, claim_expires_at,
                      updated_at
            """,
            (
                claim_token,
                claimed_at,
                claim_expires_at,
                job_id,
                *claimable_params,
            ),
        ).fetchone()
        if claimed is not None:
            self._consume_scheduler_turn(job_id, now=claimed_at)
        return claimed

    def claim_purge_group(
        self,
        *,
        lead_job_id: int,
        claim_token: str,
        claimed_at: str,
        claim_expires_at: str,
        now: str,
        message: str,
        message_key: str,
        scheduler_generation: int | None = None,
    ) -> list[dict[str, Any]]:
        """Lease every active Job in one cloud-purge group as one operation.

        A permanent purge may span many Vault Files.  Its group is only
        executable when every non-terminal member is due and unclaimed (or its
        prior lease has expired). PostgreSQL locks the group rows in deterministic
        ``requested_at, id`` order.  This prevents two workers from splitting a
        destructive group between themselves.
        """
        if not claim_token:
            raise ValueError("A Job claim token is required")
        if not self._lock_claim_vault(lead_job_id):
            return []
        # Resolve the group before taking Job locks. The advisory fence below
        # serializes different lead nominations for the same destructive group.
        lead = self.connection.execute(
            """
            SELECT vault_id, group_id
            FROM jobs
            WHERE id=%s AND action='cloud-purge'
            """,
            (lead_job_id,),
        ).fetchone()
        if lead is None or not lead.get("group_id"):
            return []
        # A contended destructive group must not block the scheduler thread.
        # Row locks below still protect cancellation/non-cooperating writers.
        advisory = self.connection.execute(
            """
            SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired
            """,
            (f"frostvault:cloud-purge:{lead['vault_id']}:{lead['group_id']}",),
        ).fetchone()
        if not advisory or not advisory["acquired"]:
            return []
        if self.connection.execute(
            """SELECT 1 FROM jobs j JOIN vaults v ON v.id=j.vault_id
               WHERE j.id=%s AND v.paused AND j.origin<>'stop-protection'""",
            (lead_job_id,),
        ).fetchone():
            return []
        if not self._lock_scheduler_turn(lead_job_id, scheduler_generation):
            return []
        expected = self.connection.execute(
            "SELECT id FROM jobs WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'",
            (lead['vault_id'], lead['group_id']),
        ).fetchall()
        rows = self.connection.execute(
            """
            SELECT id, status, pending_until, claim_token, claim_expires_at,
                   requested_at
            FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
            ORDER BY requested_at ASC, id ASC FOR UPDATE SKIP LOCKED
            """,
            (lead["vault_id"], lead["group_id"]),
        ).fetchall()
        # Never split a purge group when a status writer holds one member.
        if {row['id'] for row in rows} != {row['id'] for row in expected}:
            return []
        active = [
            row
            for row in rows
            if row["status"] not in TERMINAL_JOB_STATUSES
        ]
        if not active or any(
            not self._claim_is_available(row, now=now)
            or not self._purge_job_is_runnable(row, now=now)
            for row in active
        ):
            return []

        ids = [int(row["id"]) for row in active]
        placeholders = ", ".join(["%s"] * len(ids))
        claimed = self.connection.execute(
            f"""
            UPDATE jobs
            SET status='cleaning',
                message=%s,
                message_key=%s,
                message_params=NULL,
                claim_token=%s,
                claimed_at=%s,
                claim_expires_at=%s,
                updated_at=%s
            WHERE id IN ({placeholders})
            RETURNING id, vault_file_id, requested_by, status, claim_token,
                      claimed_at, claim_expires_at, requested_at
            """,
            (
                message,
                message_key,
                claim_token,
                claimed_at,
                claim_expires_at,
                claimed_at,
                *ids,
            ),
        ).fetchall()
        if claimed:
            self._consume_scheduler_turn(lead_job_id, now=claimed_at)
        # RETURNING order is not a SQL contract.  Preserve the scheduler's
        # deterministic ordering for the selected lead and tests.
        return sorted(
            claimed,
            key=lambda row: (str(row.get("requested_at") or ""), int(row["id"])),
        )

    def renew_job_claim(
        self,
        *,
        job_id: int,
        claim_token: str,
        now: str,
        claim_expires_at: str,
    ) -> dict[str, Any] | None:
        """Renew a live claim, returning no row after cancellation or takeover."""
        return self.connection.execute(
            """
            UPDATE jobs
            SET claim_expires_at=%s
            WHERE id=%s
              AND claim_token=%s
              AND claim_expires_at IS NOT NULL
              AND claim_expires_at > %s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            RETURNING id, status, claim_token, claimed_at, claim_expires_at,
                      updated_at
            """,
            (claim_expires_at, job_id, claim_token, now),
        ).fetchone()

    def job_claim_is_live(self, *, job_id: int, claim_token: str, now: str) -> bool:
        """Read the exact predicate of :meth:`renew_job_claim` without writing.

        The answer is the same point-in-time fact a renewal returns. It extends
        nothing, so the caller must have renewed the lease recently.
        """
        return self.connection.execute(
            """
            SELECT 1 FROM jobs
            WHERE id=%s
              AND claim_token=%s
              AND claim_expires_at IS NOT NULL
              AND claim_expires_at > %s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (job_id, claim_token, now),
        ).fetchone() is not None

    def renew_purge_group_claim(
        self,
        *,
        vault_id: int,
        group_id: str,
        claim_token: str,
        expected_job_ids: Sequence[int],
        now: str,
        claim_expires_at: str,
    ) -> bool:
        """Hold every member of a claimed purge group through one DB step."""
        rows = self.connection.execute(
            """
            UPDATE jobs
            SET claim_expires_at=%s
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
              AND status='cleaning'
              AND claim_token=%s
              AND claim_expires_at IS NOT NULL
              AND claim_expires_at > %s
            RETURNING id
            """,
            (
                claim_expires_at,
                vault_id,
                group_id,
                claim_token,
                now,
            ),
        ).fetchall()
        return {int(row["id"]) for row in rows} == {
            int(job_id) for job_id in expected_job_ids
        }

    def load_claimed_purge_group(
        self,
        *,
        lead_job_id: int,
        claim_token: str,
        now: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load a complete, still-live cloud-purge claim and its pending items."""
        lead = self.connection.execute(
            """
            SELECT vault_id, group_id
            FROM jobs
            WHERE id=%s AND action='cloud-purge'
            """,
            (lead_job_id,),
        ).fetchone()
        if lead is None or not lead.get("group_id"):
            return [], []
        all_active = self.connection.execute(
            """
            SELECT id, status, claim_token, claim_expires_at,
                   vault_file_id, requested_by
            FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
              AND status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY requested_at ASC, id ASC
            """,
            (lead["vault_id"], lead["group_id"]),
        ).fetchall()
        if not all_active or any(
            row["status"] != "cleaning"
            or row.get("claim_token") != claim_token
            or not row.get("claim_expires_at")
            or str(row["claim_expires_at"]) <= now
            for row in all_active
        ):
            return [], []
        job_ids = [int(row["id"]) for row in all_active]
        placeholders = ", ".join(["%s"] * len(job_ids))
        items = self.connection.execute(
            f"""
            SELECT * FROM cloud_deletion_items
            WHERE job_id IN ({placeholders})
              AND status IN ('pending', 'failed')
            ORDER BY id
            """,
            job_ids,
        ).fetchall()
        return all_active, items

    def claimable_queue_depth(
        self,
        *,
        now: str,
        restore_due_before: str,
        exclude_job_ids: Sequence[int] = (),
    ) -> int:
        """Count runnable, currently unleased queued Job rows.

        ``exclude_job_ids`` leaves out Jobs the worker already found not
        runnable right now, so they are not reported as work that should move.

        This is intentionally a backlog metric, not a concurrency-limited
        scheduler batch.  Leased work and stale intermediate states are absent;
        expired queued and due restoring leases are reclaimable and included.
        """
        predicate, params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE v.relocation_state='ready'
              AND (
                    (v.enabled=TRUE AND v.decommission_state='active'
                     AND j.origin IN ('manual','automatic'))
                 OR (v.decommission_state='decommissioning'
                     AND j.origin='stop-protection' AND j.action='cloud-purge')
              )
              AND ({predicate})
              AND j.id <> ALL(%s::bigint[])
            """,
            [*params, [int(job_id) for job_id in exclude_job_ids]],
        ).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    def summary(self, vault_id: int) -> dict[str, Any]:
        """Bounded SQL rollup of the same observations used by file browsing."""
        rows = self.connection.execute(
            f"""
            SELECT classified.state, COUNT(*) AS total,
                   coalesce(SUM(classified.local_bytes), 0) AS local_bytes,
                   coalesce(SUM(classified.cloud_bytes), 0) AS cloud_bytes
            FROM (
                SELECT {self._state_sql(vault_id)} AS state,
                    CASE WHEN lc.presence IN ('present', 'unsupported')
                         THEN coalesce(lc.size, 0) ELSE 0 END AS local_bytes,
                    CASE WHEN av.availability NOT IN ('missing', 'purged')
                         THEN coalesce(av.size, 0) ELSE 0 END AS cloud_bytes
                FROM vault_files vf
                JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
                {LATEST_JOIN_SQL}
                WHERE vf.vault_id=%s AND vf.status='active' AND {VISIBLE_SQL}
            ) classified GROUP BY classified.state
            """, (vault_id,),
        ).fetchall()
        states: dict[str, int] = {}
        local_bytes = 0
        cloud_bytes = 0
        for row in rows:
            state = row["state"]
            if not state:
                continue
            states[str(state)] = int(row["total"] or 0)
            local_bytes += int(row["local_bytes"] or 0)
            cloud_bytes += int(row["cloud_bytes"] or 0)
        active_jobs = self.connection.execute(
            """
            SELECT COUNT(*) AS total FROM jobs
            WHERE vault_id=%s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (vault_id,),
        ).fetchone()["total"]
        last_known_counts = dict(states)
        if self.protection_unconfirmed and states:
            states = {"unconfirmed": sum(states.values())}
        return {
            "states": states,
            "protection_state": aggregate_state(states),
            "last_known_states": last_known_counts,
            "storage": {
                "local_bytes": local_bytes,
                "cloud_bytes": cloud_bytes,
            },
            "active_jobs": int(active_jobs),
        }

    def _upload_verification_mode(self, vault_id: int) -> str:
        from .services.operation_policies import get_policy
        return get_policy(self.connection, vault_id).effective_upload_verification()

    @staticmethod
    def _carries_restore(action: str, row: Any) -> bool:
        """Only recoveries of archived classes persist a restore tier/days.

        A directory request applies one tier to every Job, but files already in
        an instant-access class never restore (#532).
        """
        if action == "storage-class":
            return True
        return action == "recover" and storage_class_requires_restore(
            row["storage_class"]
        )

    @staticmethod
    def recovery_selection_scope(selection: Sequence[tuple[str, bool]]) -> tuple[str, list[Any]]:
        """One union of paths, never a concatenation that duplicates overlaps.

        Callers normalize paths first. The surrounding query must bind vf.vault_id.
        starts_with treats SQL wildcard characters literally.
        """
        clauses, params = [], []
        for path, is_directory in selection:
            clauses.append("starts_with(fp.path, %s)" if is_directory else "fp.path=%s")
            params.append(path + "/" if is_directory else path)
        return "(" + " OR ".join(clauses) + ")", params

    def queue_jobs(
        self,
        *,
        vault_id: int,
        path: str,
        action: str,
        requested_by: int,
        requested_at: str,
        group_id: str,
        is_directory: bool,
        archive_version_id: str | None = None,
        restore_tier: str | None = None,
        restore_days: int | None = None,
        estimated_cost_eur: float | None = None,
        estimated_hours: float | None = None,
        initial_status: str = "queued",
        pending_until: str | None = None,
        origin: str = "manual",
        target_storage_class: str | None = None,
        whole_vault: bool = False,
        recovery_selection: Sequence[tuple[str, bool]] | None = None,
        admission_path_ids: Sequence[int] | None = None,
        admission_verification_mode: str | None = None,
    ) -> tuple[list[int], int, int]:
        # This write is deliberately first: take the Vault row lock before
        # the candidate read and inserts.
        # Every manual upload/recovery/free-space admission therefore observes
        # one serialized Vault state.
        lock_vault(self.connection, vault_id)
        self.last_skipped_same_class = 0
        if origin not in {"manual", "automatic"}:
            raise ValueError("Unsupported Job origin")
        clauses = ["vf.vault_id=%s", "fp.vault_id=vf.vault_id", "vf.status='active'"]
        params: list[Any] = [vault_id]
        if admission_path_ids is not None:
            if action != "upload" or origin != "manual":
                raise ValueError("Bounded admission is only for manual uploads")
            clauses.append("fp.id=ANY(%s::bigint[])")
            params.append(list(admission_path_ids))
        if admission_verification_mode not in {None, "s3_checksum", "read_back"}:
            raise ValueError("Invalid admission verification mode")
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM jobs pending
                WHERE pending.vault_file_id=vf.id
                  AND pending.status NOT IN (%s, %s, %s)
            )
            """
        )
        params.extend(TERMINAL_JOB_STATUSES)
        if recovery_selection:
            if action != "recover":
                raise ValueError("Selections are only supported for recovery")
            scope, scope_params = self.recovery_selection_scope(recovery_selection)
            clauses.append(scope)
            params.extend(scope_params)
        elif whole_vault:
            pass
        elif is_directory:
            escaped = (
                path.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("fp.path LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}/%")
        else:
            clauses.append("fp.path=%s")
            params.append(path)
        if action == "recover" and archive_version_id:
            # Exact IDs pin the current snapshot; they cannot select history,
            # including legacy available copies retained fail-closed at upgrade.
            clauses.append("""
                av.id=(SELECT current_snapshot.id FROM archive_versions current_snapshot
                       WHERE current_snapshot.vault_file_id=vf.id
                         AND current_snapshot.integrity='verified'
                         AND current_snapshot.availability='available'
                       ORDER BY current_snapshot.version_number DESC LIMIT 1)
            """)
        if action in {"recover", "storage-class"} and archive_version_id and not is_directory and not whole_vault:
            candidates = self.connection.execute(
                f"""
                SELECT
                    vf.id AS vault_file_id,
                    fp.path,
                    lc.presence,
                    lc.file_type,
                    lc.size AS local_size,
                    lc.plaintext_sha256 AS local_sha256,
                    lc.matched_archive_version_id,
                    lc.cloud_only_version_id,
                    av.id AS archive_version_id,
                    av.size AS cloud_size,
                    av.plaintext_sha256 AS version_sha256,
                    av.integrity,
                    av.availability,
                    av.storage_class,
                    av.restore_state
                FROM vault_files vf
                JOIN file_paths fp
                  ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
                JOIN archive_versions av ON av.id=%s AND av.vault_file_id=vf.id
                WHERE {" AND ".join(clauses)}
                ORDER BY lower(fp.path)
                """,
                [archive_version_id, *params],
            ).fetchall()
        else:
            candidates = self.connection.execute(
                f"""
                SELECT
                    vf.id AS vault_file_id,
                    fp.path,
                    lc.presence,
                    lc.file_type,
                    lc.size AS local_size,
                    lc.plaintext_sha256 AS local_sha256,
                    lc.matched_archive_version_id,
                    lc.cloud_only_version_id,
                    av.id AS archive_version_id,
                    av.size AS cloud_size,
                    av.plaintext_sha256 AS version_sha256,
                    av.integrity,
                    av.availability,
                    av.storage_class,
                    av.restore_state
                FROM vault_files vf
                JOIN file_paths fp
                  ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
                LEFT JOIN archive_versions av
                  ON av.id=(
                      SELECT latest.id
                      FROM archive_versions latest
                      WHERE latest.vault_file_id=vf.id
                        AND latest.availability NOT IN ('missing', 'purged')
                        AND (
                            %s NOT IN ('recover', 'storage-class')
                            OR (
                                latest.availability='available'
                                AND (
                                    %s <> 'recover'
                                    OR latest.integrity='verified'
                                )
                            )
                        )
                      ORDER BY latest.version_number DESC
                      LIMIT 1
                  )
                WHERE {" AND ".join(clauses)}
                ORDER BY lower(fp.path)
                """,
                [action, action, *params],
            ).fetchall()
        eligible = []
        remediation_job_ids: list[int] = []
        skipped_same = 0
        target_class = (target_storage_class or "").upper() or None
        for row in candidates:
            if action == "upload":
                # Cloud history is not protection. Admit a new Archive Version
                # when the current Local Copy is not backed by a verified,
                # available, digest-matching version. Unchanged protected
                # content stays ineligible so duplicates are not queued.
                allowed = local_copy_is_upload_eligible(row)
                if allowed:
                    from .services import upload_remediation
                    handled, resumed = upload_remediation.intercept_upload(
                        self.connection, vault_id=vault_id, file_id=row["vault_file_id"],
                        user_id=requested_by, stamp=requested_at,
                        automatic=origin == "automatic", group_id=group_id,
                    )
                    if handled:
                        if resumed:
                            remediation_job_ids.append(int(resumed["job_id"]))
                            self._mark_path_aggregates_dirty(vault_id, row["path"])
                        allowed = False
            elif action == "recover":
                allowed = (
                    row["presence"] != "present"
                    and row["archive_version_id"] is not None
                    and row["integrity"] == "verified"
                    and row["availability"] == "available"
                )
            elif action == "free-space":
                allowed = (
                    row["presence"] == "present"
                    and row["file_type"] == "regular"
                    and row["archive_version_id"] is not None
                    and row["integrity"] == "verified"
                    and row["availability"] == "available"
                    and row["matched_archive_version_id"]
                    == row["archive_version_id"]
                    and row["local_sha256"] is not None
                    and row["local_sha256"] == row["version_sha256"]
                )
            elif action == "rename":
                # Confirmed Path History already points at the new path. A rename
                # job migrates verified cloud content to that path's key when a
                # prior verified Archive Version still exists.
                allowed = (
                    row["presence"] == "present"
                    and row["file_type"] == "regular"
                    and row["archive_version_id"] is not None
                    and row["integrity"] == "verified"
                    and row["availability"] == "available"
                )
            elif action == "storage-class":
                allowed = (
                    row["archive_version_id"] is not None
                    and row["availability"] == "available"
                    and row["restore_state"] != "restoring"
                )
                if allowed and target_class:
                    current = (row["storage_class"] or "STANDARD").upper()
                    if current == target_class:
                        skipped_same += 1
                        allowed = False
            else:
                raise ValueError(f"Unsupported job action: {action}")
            if allowed:
                eligible.append(row)
        self.last_skipped_same_class = skipped_same

        job_ids: list[int] = list(remediation_job_ids)
        total_bytes = sum(int(row["total_bytes"] or 0) for row in self.connection.execute(
            "SELECT total_bytes FROM jobs WHERE id=ANY(%s)", (remediation_job_ids,),
        ).fetchall()) if remediation_job_ids else 0
        upload_mode = (admission_verification_mode or self._upload_verification_mode(vault_id)) if action == "upload" else None
        for row in eligible:
            size = int(
                row["cloud_size"]
                if action in {"recover", "storage-class"}
                else row["local_size"] or 0
            )
            job = self.connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, archive_version_id, path,
                    action, status, requested_by, requested_at, updated_at,
                    group_id, group_path, total_bytes, transferred_bytes,
                    restore_tier, restore_days, estimated_cost_eur,
                    estimated_hours, pending_until, origin, target_storage_class,
                    upload_verification_requested
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, 0,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    vault_id,
                    row["vault_file_id"],
                    (
                        None
                        if action == "upload"
                        else row["archive_version_id"]
                    ),
                    row["path"],
                    action,
                    initial_status,
                    requested_by,
                    requested_at,
                    requested_at,
                    group_id,
                    path if path else (row["path"] if not whole_vault else ""),
                    size,
                    restore_tier if self._carries_restore(action, row) else None,
                    restore_days if self._carries_restore(action, row) else None,
                    estimated_cost_eur if action in {"recover", "storage-class"} else None,
                    estimated_hours if action in {"recover", "storage-class"} else None,
                    pending_until if action == "recover" else None,
                    origin,
                    target_class if action == "storage-class" else None,
                    upload_mode,
                ),
            ).fetchone()
            if job is None:
                continue
            job_ids.append(int(job["id"]))
            if action == "upload":
                self._mark_path_aggregates_dirty(vault_id, row["path"])
            total_bytes += size
        return job_ids, total_bytes, len(eligible) + len(remediation_job_ids)
