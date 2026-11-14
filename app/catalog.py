from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .services.job_admission import (
    JOB_LEASE_HELD_STATUSES,
    TERMINAL_JOB_STATUSES,
    lock_vault,
)
from .services.s3_copy import storage_class_requires_restore
from .services.protection_states import (
    CURRENT_JOIN_SQL, PROTECTED_SQL, VISIBLE_SQL, aggregate_state, current_is_recoverable,
    local_copy_matches_current, path_excluded_sql, project_file, state_sql,
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
    stopped_upload_counts,
)
from .services.operation_policies import stopped_upload_sql


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


# The verification the Owner requested for Current's upload, read from the
# Upload Snapshot its landed attempt sent (#588), never copied onto the version.
_REQUESTED_VERIFICATION_SQL = """
    SELECT snapshot.verification FROM upload_attempts attempt
    JOIN upload_snapshots snapshot ON snapshot.id = attempt.snapshot_id
    WHERE attempt.id = cur.upload_attempt_id
"""


def _cleanup_state(row: Mapping[str, Any]) -> str | None:
    """Exact cleanup of superseded versions: in progress, blocked or waiting.

    A frozen batch (#591) is in progress; a reconciliation block on a file
    with superseded versions or a batch means the cleanup is blocked and needs
    the Owner. Superseded versions without a batch wait for the next upload
    Job of the file. None: only Current (and a replacement) remain.
    """
    if row["cleanup_batch_id"] is None and not row["has_superseded"]:
        return None
    if row["reconciliation_block"] is not None:
        return "blocked"
    return "in_progress" if row["cleanup_batch_id"] is not None else "waiting"


def _restore_expiry(value: Any) -> datetime | None:
    """The provider's restore expiry (an HTTP date or ISO text), or None."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


def awaiting_rename_analysis_sql(alias: str) -> str:
    """A new Local Copy that may be a Local Copy renamed without a scan (#621).

    It never had cloud content or a Job, has no fingerprint yet, and has the
    size of a fingerprinted Local Copy that went missing within the rename
    window. Its digest must decide whether it is that file before any
    automatic upload treats it as a new Vault File (ADR-0001).
    """
    window = int(AUTO_RENAME_WINDOW.total_seconds())
    return f"""(
        {alias}.local_sha256 IS NULL
        AND {alias}.cloud_since IS NULL
        AND {alias}.held_version_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM jobs own WHERE own.file_id={alias}.id)
        AND EXISTS (
            SELECT 1 FROM files source
            WHERE source.vault_id={alias}.vault_id AND source.id<>{alias}.id
              AND source.local_presence='missing'
              AND source.local_file_type='regular'
              AND source.local_sha256 IS NOT NULL
              AND source.held_version_id IS NULL
              AND source.local_size={alias}.local_size
              AND source.local_observed_at
                  BETWEEN {alias}.local_observed_at - interval '{window} seconds'
                      AND {alias}.local_observed_at + interval '{window} seconds'
        )
    )"""


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

        The file's generation measures content observations: it increments
        only when presence, kind, size or mtime change, or when
        ``invalidate_content`` (a watcher content event) says the bytes may
        have changed anyway, so a hash taken on an earlier observation can no
        longer publish. A re-observation that confirms the same copy keeps
        the generation, so it never fences an upload or a hash in flight
        (#619). The fingerprint and match are kept only for a present regular
        file whose stat is unchanged and that had no content event.
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
        # The content observation a scan re-confirms leaves the generation
        # alone (#619); a content event via ``invalidate_paths`` changes it.
        content_changed = """
            (f.local_presence, f.local_file_type, f.local_size, f.local_mtime_ns)
            IS DISTINCT FROM
            (EXCLUDED.local_presence, EXCLUDED.local_file_type,
             EXCLUDED.local_size, EXCLUDED.local_mtime_ns)
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
                generation = CASE
                    WHEN {content_changed} OR EXCLUDED.path = ANY(%s::text[])
                    THEN f.generation + 1
                    ELSE f.generation
                END,
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
                    WHEN {content_changed}
                    THEN EXCLUDED.local_observed_at
                    ELSE COALESCE(f.local_content_observed_at, EXCLUDED.local_observed_at)
                END,
                local_recovered_at = COALESCE(EXCLUDED.local_recovered_at, f.local_recovered_at)
            RETURNING f.path, f.id
            """,
            (
                vault_id, recovered,
                paths, presences, types, sizes, mtimes, observed, seen_values,
                sorted(set(invalidate_paths)),
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

    def local_copies_to_identify(self, vault_id: int) -> list[dict[str, Any]]:
        """Unfingerprinted Local Copies whose digest decides what they are (#621).

        Content back at a cloud-only path (identical: the Local Copy again;
        different: a Cloud-only Conflict) and a new Local Copy that may be a
        rename. The watcher hashes them once stable, outside any write
        transaction, and publishes with the ``generation`` read here.
        """
        rows = self.connection.execute(
            f"""
            SELECT f.id, f.path, f.generation, f.local_size, f.local_mtime_ns,
                   COALESCE(f.local_content_observed_at, f.local_observed_at)
                       AS content_observed_at,
                   f.held_version_id IS NOT NULL AS held
            FROM files f
            WHERE f.vault_id=%s
              AND f.local_presence='present'
              AND f.local_file_type='regular'
              AND f.local_sha256 IS NULL
              AND (f.held_version_id IS NOT NULL OR {awaiting_rename_analysis_sql("f")})
            ORDER BY f.path
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
                "content_observed_at": row["content_observed_at"],
                "held": bool(row["held"]),
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
        match_current: bool = False,
    ) -> bool:
        """Publish a digest for the observation it was computed from.

        Compare-and-swap on the generation read **before** hashing and on the
        stat the hash saw: a newer observation, a rename or a different stat
        wins and the digest is discarded (False). Content that reappeared at a
        cloud-only path and proves identical to Current becomes matched and
        is released from its hold; different content stays held.
        ``match_current`` (the upload worker, which proved the digest itself)
        also matches a Local Copy whose digest equals the verified, available
        Current; only Current, never an older version.
        """
        digest = _digest(plaintext_sha256)
        current = """WHEN %(match_current)s THEN (
                SELECT cur.id FROM archive_versions cur
                WHERE cur.id = f.current_version_id
                  AND cur.integrity = 'verified'
                  AND cur.availability = 'available'
                  AND cur.plaintext_sha256 = %(digest)s
            )"""
        settled = f"""CASE
            WHEN f.held_version_id IS NOT NULL THEN (
                SELECT held.id FROM archive_versions held
                WHERE held.id = f.held_version_id
                  AND held.integrity = 'verified'
                  AND held.availability = 'available'
                  AND held.plaintext_sha256 = %(digest)s
            )
            WHEN f.local_sha256 = %(digest)s AND f.matched_version_id IS NOT NULL
            THEN f.matched_version_id
            {current}
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
                "match_current": bool(match_current),
            },
        ).fetchone()
        if row is None:
            return False
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])
        return True

    def publish_recovered_local_copy(
        self,
        vault_file_id: str,
        *,
        generation: int,
        archive_version_id: str,
        plaintext_sha256: str | bytes,
        size: int,
        mtime_ns: int,
        recovered_at: Any,
    ) -> dict[str, Any] | None:
        """Publish a recovered Local Copy: observation, digest and ``recovered_at``.

        One compare-and-swap (#594) on the generation the recovery read, under
        the scan lock, before it published the file: a newer observation or a
        rename wins and nothing is written (None). The digest is the one the
        recovery proved against the exact Archive Version; the copy is matched
        only when that version is still the file's verified, available
        Current, never an older one. Content that is not Current's stays held
        when Current is the only copy, like any reappearance.
        """
        digest = _digest(plaintext_sha256)
        stamp = _required_timestamp(recovered_at)
        matched = """(
            SELECT cur.id FROM archive_versions cur
            WHERE cur.id = f.current_version_id
              AND cur.id = %(version)s
              AND cur.integrity = 'verified'
              AND cur.availability = 'available'
              AND cur.plaintext_sha256 = %(digest)s
        )"""
        row = self.connection.execute(
            f"""
            UPDATE files f
            SET generation = f.generation + 1,
                local_presence = 'present', local_file_type = 'regular',
                local_size = %(size)s, local_mtime_ns = %(mtime_ns)s,
                local_sha256 = %(digest)s,
                matched_version_id = {matched},
                held_version_id = CASE
                    WHEN {matched} IS NULL THEN {_only_cloud_copy_sql("f")}
                END,
                pending_since = CASE
                    WHEN {matched} IS NOT NULL THEN NULL ELSE f.pending_since
                END,
                local_seen_at = %(stamp)s, local_observed_at = %(stamp)s,
                local_content_observed_at = %(stamp)s,
                local_recovered_at = %(stamp)s
            WHERE f.id = %(id)s
              AND f.generation = %(generation)s
              AND f.local_presence IS DISTINCT FROM 'present'
            RETURNING f.vault_id, f.path, f.matched_version_id
            """,
            {
                "digest": digest, "version": archive_version_id, "stamp": stamp,
                "size": int(size), "mtime_ns": int(mtime_ns), "id": vault_file_id,
                "generation": int(generation),
            },
        ).fetchone()
        if row is None:
            return None
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])
        return dict(row)

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
        pending lose theirs. Returns the durable ``{file_id: pending_since}``
        of the files still pending at the write: a file protected after the
        caller read it (a concurrent upload published Current and cleared its
        timer) is left out and never gets its timer back (#549).
        """
        ids = list(pending)
        # One read of every timer of the Vault. A write happens only for a
        # timer that starts or ends: an unchanged pending file is never
        # rewritten (#622), so a long queue costs no WAL per call.
        rows = self.connection.execute(
            f"""
            SELECT f.id, f.pending_since, (f.id = ANY(%s::uuid[])) AS wanted,
                   (f.local_presence = 'present' AND f.held_version_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM archive_versions cur
                        WHERE cur.id = f.current_version_id AND {PROTECTED_SQL}
                    )) AS eligible
            FROM files f
            WHERE f.vault_id = %s AND f.pending_since IS NOT NULL
            """,
            (ids, vault_id),
        ).fetchall()
        durable = {str(row["id"]): row["pending_since"]
                   for row in rows if row["wanted"] and row["eligible"]}
        timed = {str(row["id"]) for row in rows}
        ended = [str(row["id"]) for row in rows if not row["wanted"]]
        if ended:
            self.connection.execute(
                """
                UPDATE files SET pending_since=NULL
                WHERE vault_id=%s AND id = ANY(%s::uuid[]) AND pending_since IS NOT NULL
                """,
                (vault_id, ended),
            )
        starting = [file_id for file_id in ids if file_id not in timed]
        if starting:
            sinces = [_required_timestamp(pending[file_id]) for file_id in starting]
            # Re-checked under the row lock: a file protected after the caller
            # read it keeps no timer, and a timer set meanwhile is not reset.
            started = self.connection.execute(
                f"""
                UPDATE files f
                SET pending_since = s.since
                FROM unnest(%s::uuid[], %s::timestamptz[]) AS s(id, since)
                WHERE f.id = s.id AND f.vault_id = %s AND f.pending_since IS NULL
                  AND f.local_presence = 'present' AND f.held_version_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM archive_versions cur
                      WHERE cur.id = f.current_version_id AND {PROTECTED_SQL}
                  )
                RETURNING f.id, f.pending_since
                """,
                (starting, sinces, vault_id),
            ).fetchall()
            durable.update((str(row["id"]), row["pending_since"]) for row in started)
        return durable

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
        upload_attempt_id: str | None = None,
    ) -> str:
        """Record the uploaded object as this file's one replacement in progress.

        The Archive Version is born unverified and available at its exact
        provider identity; Current is unchanged until
        :meth:`promote_replacement`. Raises :class:`ReplacementInProgress` when
        another replacement is already in progress (nothing is written).
        ``upload_attempt_id`` names the landed upload attempt that produced it:
        its snapshot is the version's expected proof (#588), and the database
        refuses any key, size or VersionId other than the attempt's.
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
                    availability, availability_checked_at, created_at, upload_attempt_id
                )
                SELECT target.id, %s, %s, %s, %s, 'available', %s, %s, %s FROM target
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
                storage_class, stamp, stamp, upload_attempt_id, stamp,
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
        replacement_locked: bool = False,
    ) -> bool:
        """Verify the replacement in progress and make it Current, atomically.

        This is the only way Current changes. The superseded version stays
        unreferenced until its exact cleanup removes it (#591). The Local Copy
        becomes matched only when it is still the observation
        ``local_generation`` and its fingerprint equals the verified digest.
        Returns whether the Local Copy is matched to the new Current.

        ``replacement_locked``: the caller read this file row FOR UPDATE in
        this transaction and saw ``archive_version_id`` as its replacement.
        """
        digest = _digest(plaintext_sha256)
        stamp = _required_timestamp(verified_at)
        if not replacement_locked:
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
        if row is None:
            raise StaleReplacement("The Archive Version is not the replacement in progress")
        self._mark_path_aggregates_dirty(int(row["vault_id"]), row["path"])
        return row["matched_version_id"] is not None

    def abandon_replacement(
        self,
        vault_file_id: str,
        *,
        archive_version_id: str,
        mismatch_sha256: str | bytes | None = None,
        checked_at: Any | None = None,
        mismatch: bool = False,
    ) -> None:
        """End the replacement in progress without touching Current.

        With ``mismatch_sha256`` (the digest a read-back proved) or
        ``mismatch`` (contradictory provider checksum evidence, no digest) the
        version is recorded as a proven mismatch. The version stays as
        superseded until its exact cleanup (#591): a failed upload or
        verification always preserves the previous copy.
        """
        if mismatch_sha256 is not None or mismatch:
            self.connection.execute(
                """
                UPDATE archive_versions
                SET integrity='mismatch', plaintext_sha256=%s, verified_at=%s
                WHERE id=%s AND file_id=%s AND integrity='unverified'
                """,
                (
                    None if mismatch_sha256 is None else _digest(mismatch_sha256),
                    _required_timestamp(checked_at), archive_version_id, vault_file_id,
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
        self, *, vault_id: int, scan_id: Any, scan_started_at: Any,
        superseded: bool = True,
    ) -> list[dict[str, Any]]:
        """Mark unseen versions missing and return the new transitions.

        Versions with a concurrent upload Job on their file, versions seen in
        this scan and versions created after it started are left unchanged so
        they cannot open false loss incidents. With ``superseded=False`` only
        Current and the replacement in progress can become missing: a
        superseded version that is gone is not a loss (#639).
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
              AND (%s OR av.id = f.current_version_id
                   OR av.id = f.replacing_version_id)
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
                _required_timestamp(scan_started_at), bool(superseded), terminal,
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

    # -- Reconnect after a metadata restore ---------------------------------

    def adopt_cloud_path(self, vault_id: int, path: str, *, at: Any) -> str:
        """The Vault File at ``path``, created with no Local Copy if absent.

        Used only by the cloud reconciliation of a metadata restore (#638):
        content uploaded after the backup has no row in the restored catalog.
        The Local Copy stays unobserved until the local rehash finds it.
        """
        self._vault_lock_touch(vault_id)
        row = self.connection.execute(
            """
            INSERT INTO files(vault_id, path, path_since) VALUES (%s, %s, %s)
            ON CONFLICT (vault_id, path) DO NOTHING
            RETURNING id
            """,
            (vault_id, path, _required_timestamp(at)),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT id FROM files WHERE vault_id=%s AND path=%s", (vault_id, path),
            ).fetchone()
        self._mark_path_aggregates_dirty(vault_id, path)
        return str(row["id"])

    def close_absent_superseded_versions(
        self, *, vault_id: int, scan_id: Any, provider: str,
        source_root: str, root_identity: str | None,
    ) -> list[str]:
        """Close superseded versions a complete listing proved gone (#639).

        After a metadata restore the catalog still holds versions that the
        Current Snapshot cleanup deleted after the backup. Their cleanup is
        complete: they are recorded as found absent in a cleanup batch kept
        by the verified Current and removed with that proof, exactly as a
        finished cleanup does. Only landed application writes of a file
        without an open cleanup batch qualify; any other superseded version
        is left untouched. Returns the closed Archive Version ids.
        """
        stamp = _required_timestamp(scan_id)
        rows = self.connection.execute(
            """
            SELECT av.id, f.id AS file_id, f.current_version_id, f.path_since
            FROM files f
            JOIN archive_versions cur
              ON cur.id = f.current_version_id AND cur.integrity = 'verified'
            JOIN archive_versions av ON av.file_id = f.id
            LEFT JOIN upload_attempts a ON a.id = av.upload_attempt_id
            LEFT JOIN copy_attempts ca ON ca.id = av.copy_attempt_id
            WHERE f.vault_id = %s
              AND av.id <> f.current_version_id
              AND av.id IS DISTINCT FROM f.replacing_version_id
              AND av.integrity <> 'mismatch'
              AND av.availability_checked_at IS DISTINCT FROM %s
              AND av.created_at <= %s
              AND ((a.outcome = 'landed' AND a.landed_version_id = av.provider_version_id)
                   OR (ca.outcome = 'landed'
                       AND ca.landed_version_id = av.provider_version_id))
              AND NOT EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.file_id = f.id)
            ORDER BY f.id, av.id
            FOR UPDATE OF f
            """,
            (vault_id, stamp, stamp),
        ).fetchall()
        by_file: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_file.setdefault(str(row["file_id"]), []).append(row)
        closed: list[str] = []
        for file_id, versions in by_file.items():
            batch = self.connection.execute(
                """
                INSERT INTO cleanup_batches(vault_id, file_id, keeper_id, provider,
                                            path_since, source_root, root_identity)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (
                    vault_id, file_id, versions[0]["current_version_id"], provider,
                    versions[0]["path_since"], source_root, root_identity,
                ),
            ).fetchone()["id"]
            ids = [str(row["id"]) for row in versions]
            self.connection.execute(
                """
                INSERT INTO cleanup_candidates(batch_id, file_id, archive_version_id)
                SELECT %s, %s, unnest(%s::uuid[])
                """,
                (batch, file_id, ids),
            )
            # Found absent by the complete listing: the deletion is done.
            self.connection.execute(
                "UPDATE cleanup_candidates SET deleted_at=now() WHERE batch_id=%s",
                (batch,),
            )
            self.connection.execute(
                "DELETE FROM archive_versions WHERE id = ANY(%s::uuid[])", (ids,),
            )
            self.connection.execute("DELETE FROM cleanup_batches WHERE id=%s", (batch,))
            self._mark_file_aggregates_dirty(vault_id, file_id)
            closed.extend(ids)
        return closed

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

    def link_job_version(self, job_id: int, archive_version_id: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET archive_version_id=%s WHERE id=%s",
            (archive_version_id, job_id),
        )

    def record_verification_evidence(
        self, version_id: str, *, method: str, fallback: str | None,
        egress_bytes: int | None,
    ) -> None:
        """How the version's digest is being proven: written while unverified.

        Recorded before a read-back GET so its cost is visible even when the
        read fails; once verified, the evidence is history, never rewritten.
        """
        self.connection.execute(
            """
            UPDATE archive_versions
            SET verification_method=%s, verification_fallback=%s,
                verification_egress_bytes=%s
            WHERE id=%s AND integrity='unverified'
            """,
            (method, fallback, egress_bytes, version_id),
        )

    def get_job_target(self, job_id: int) -> dict[str, Any] | None:
        """The Job's File, Local Copy and linked Archive Version in one row.

        Digests are hexadecimal text. ``upload_snapshot_json`` is the linked
        version's expected proof, reached through its upload attempt (#588);
        it is never copied onto the version.
        """
        row = self.connection.execute(
            """
            SELECT
                j.id AS job_id,
                j.origin AS job_origin,
                j.vault_id,
                j.file_id,
                j.file_id AS vault_file_id,
                j.archive_version_id,
                ju.verification_mode AS job_upload_verification_requested,
                COALESCE(ju.reconcile_only, false) AS upload_reconcile_only,
                ju.snapshot_id AS upload_snapshot_id,
                f.path,
                f.generation AS local_generation,
                f.local_presence,
                f.local_file_type,
                f.local_size,
                f.local_mtime_ns,
                f.local_recovered_at,
                COALESCE(f.local_content_observed_at, f.local_observed_at) AS content_observed_at,
                encode(f.local_sha256, 'hex') AS local_sha256,
                f.matched_version_id AS matched_archive_version_id,
                f.held_version_id AS cloud_only_version_id,
                f.current_version_id,
                f.replacing_version_id,
                av.object_key,
                av.provider_version_id,
                av.size AS cloud_size,
                av.storage_class,
                encode(av.plaintext_sha256, 'hex') AS version_sha256,
                av.integrity,
                av.availability,
                av.verified_at,
                av.verification_method,
                av.verification_fallback AS verification_fallback_reason,
                av.verification_egress_bytes,
                av.upload_attempt_id,
                av.restore_state,
                av.restore_expiry,
                jr.tier AS restore_tier,
                jr.days AS restore_days
            FROM jobs j
            JOIN files f ON f.id = j.file_id
            LEFT JOIN job_uploads ju ON ju.job_id = j.id
            LEFT JOIN job_restores jr ON jr.job_id = j.id
            LEFT JOIN archive_versions av ON av.id = j.archive_version_id
            WHERE j.id=%s
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        target = dict(row)
        target["upload_snapshot_json"] = None
        target["upload_verification_requested"] = None
        if target["archive_version_id"] is not None:
            from .services.upload_provenance import expected_for_version
            expected = expected_for_version(self.connection, str(target["archive_version_id"]))
            if expected is not None:
                target["upload_snapshot_json"] = expected.snapshot.as_json()
                target["upload_verification_requested"] = expected.verification
        return target

    def update_restore_state(
        self,
        archive_version_id: str,
        *,
        state: str | None,
        expiry: Any,
        checked_at: Any,
        storage_class: str | None = None,
    ) -> None:
        """Record the observed cloud restore of one exact Archive Version.

        ``state`` is ``restoring``, ``available`` or None (no restore needed or
        observed); ``expiry`` is the provider's expiry of the temporary copy,
        kept only while it is available. Integrity and availability are never
        touched (#594).
        """
        if state == "not_requested":
            state = None
        row = self.connection.execute(
            """
            UPDATE archive_versions av
            SET restore_state=%s,
                restore_expiry=CASE WHEN %s::text='available' THEN %s::timestamptz END,
                restore_checked_at=%s,
                storage_class=COALESCE(%s, av.storage_class)
            FROM files f
            WHERE av.id=%s AND f.id=av.file_id
            RETURNING f.vault_id, f.id AS file_id
            """,
            (state, state, _restore_expiry(expiry), _required_timestamp(checked_at),
             storage_class, archive_version_id),
        ).fetchone()
        if row is not None:
            self._mark_file_aggregates_dirty(int(row["vault_id"]), str(row["file_id"]))

    # -- Projections: browse, search, versions (#593) ----------------------------
    #
    # Every listing reads one ``files`` row per Vault File and its explicit
    # Current (``cur``); the replacement in progress is the explicit pending
    # pointer. Nothing is ever chosen as "the most recent version" by ordering.

    def list_versions(self, vault_id: int, path: str) -> list[dict[str, Any]]:
        """Current and the replacement in progress of the file at ``path``.

        These are the only Archive Versions a Vault File can have besides
        superseded versions awaiting exact cleanup, which are never offered.
        Each row names its role explicitly; only Current can be recoverable.
        """
        rows = self.connection.execute(
            f"""
            SELECT role.role, av.id, av.object_key, av.provider_version_id,
                   av.size, av.storage_class, av.plaintext_sha256, av.integrity,
                   av.availability, av.availability_checked_at, av.verified_at,
                   av.restore_state, av.restore_expiry, av.verification_method, av.verification_fallback,
                   av.verification_egress_bytes, av.created_at,
                   ({_REQUESTED_VERIFICATION_SQL.replace("cur.", "av.")})
                       AS upload_verification_requested
            FROM files f
            CROSS JOIN LATERAL (VALUES
                ('current', f.current_version_id),
                ('pending', f.replacing_version_id)
            ) AS role(role, version_id)
            JOIN archive_versions av ON av.id = role.version_id
            WHERE f.vault_id=%s AND f.path=%s
            ORDER BY role.role
            """,
            (vault_id, path),
        ).fetchall()
        versions = []
        for row in rows:
            recoverable = (
                row["role"] == "current"
                and row["integrity"] == "verified"
                and row["availability"] == "available"
            )
            versions.append({
                "id": str(row["id"]),
                "role": row["role"],
                "object_key": row["object_key"],
                "provider_version_id": row["provider_version_id"],
                "size": row["size"],
                "storage_class": row["storage_class"],
                "plaintext_sha256": _hex(row["plaintext_sha256"]),
                "integrity": row["integrity"],
                "availability": row["availability"],
                "availability_checked_at": row["availability_checked_at"],
                "verified_at": row["verified_at"],
                "restore_state": row["restore_state"],
                "restore_expiry": row["restore_expiry"],
                "created_at": row["created_at"],
                "upload_verification_requested": row["upload_verification_requested"],
                "verification_method": row["verification_method"],
                "verification_fallback_reason": row["verification_fallback"],
                "verification_egress_bytes": row["verification_egress_bytes"],
                "recoverable": recoverable,
                "not_selectable_reason": None if recoverable else (
                    "pending" if row["role"] == "pending"
                    else row["availability"] if row["availability"] != "available"
                    else row["integrity"]
                ),
            })
        return versions

    def list_file_rows(
        self,
        vault_id: int,
        *,
        search: str = "",
        path_prefix: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["f.vault_id=%s"]
        params: list[Any] = [vault_id]
        if search:
            clauses.append("lower(f.path) LIKE lower(%s)")
            params.append(f"%{search}%")
        if path_prefix:
            clauses.append("starts_with(f.path, %s)")
            params.append(f"{path_prefix}/")
        clauses.append(VISIBLE_SQL)
        rows = self.connection.execute(
            self._file_row_select_sql(clauses, vault_id) + " ORDER BY lower(f.path), f.path", params,
        ).fetchall()
        return self._materialize_file_items(vault_id, rows, name_from_path=True)

    def get_file_item(self, vault_id: int, path: str) -> dict[str, Any] | None:
        """The browse item of the visible file at exactly ``path``."""
        rows = self.connection.execute(
            self._file_row_select_sql(["f.vault_id=%s", "f.path=%s", VISIBLE_SQL], vault_id),
            (vault_id, path),
        ).fetchall()
        items = self._materialize_file_items(vault_id, rows, name_from_path=True)
        return items[0] if items else None

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
            branches.append(f"SELECT scoped.*, %s::text AS vault_name FROM ({sql}) scoped")
            params.extend([vault["name"], *values])
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
        stopped: dict[str, int] = {}
        if self.protection_unconfirmed:
            folder_state = "" if not state or state == "unconfirmed" else "__none__"
        else:
            # The projection holds Stopped Uploads as pending (#630).
            from .services.operation_policies import get_policy
            stopped = stopped_upload_counts(
                self.connection, vault_id, parent_path=parent_path,
                policy=get_policy(self.connection, vault_id),
            )
        folder_total = count_child_directories(
            self.connection,
            vault_id,
            parent_path=parent_path,
            state_filter=folder_state,
            stopped=stopped,
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
                stopped=stopped,
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
        clauses = ["f.vault_id=%s"]
        params: list[Any] = [vault_id]
        if search:
            clauses.append("lower(f.path) LIKE lower(%s)")
            params.append(f"%{search}%")
        elif directory:
            # Immediate children only; starts_with treats wildcards literally.
            clauses.append("starts_with(f.path, %s)")
            params.append(f"{directory}/")
            clauses.append("strpos(substr(f.path, %s), '/') = 0")
            params.append(len(directory) + 2)
        else:
            clauses.append("strpos(f.path, '/') = 0")
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
                {stopped_upload_sql("f")} AS upload_stopped,
                CASE WHEN cur.availability='unknown'
                     THEN greatest(f.local_observed_at, cur.verified_at)
                     ELSE greatest(f.local_observed_at, cur.verified_at, cur.availability_checked_at)
                END AS protection_observed_at,
                f.id AS file_id,
                f.vault_id,
                f.path,
                f.local_presence,
                f.local_file_type,
                f.local_size,
                f.local_mtime_ns,
                f.local_sha256,
                f.matched_version_id,
                f.held_version_id,
                f.reconciliation_block,
                f.current_version_id,
                f.replacing_version_id,
                cur.size AS cloud_size,
                cur.storage_class,
                cur.plaintext_sha256 AS version_sha256,
                cur.integrity,
                cur.availability,
                cur.verification_method,
                cur.verification_fallback,
                cur.verification_egress_bytes,
                ({_REQUESTED_VERIFICATION_SQL}) AS upload_verification_requested,
                cleanup.id AS cleanup_batch_id,
                EXISTS (
                    SELECT 1 FROM archive_versions superseded
                    WHERE superseded.file_id = f.id
                      AND superseded.id IS DISTINCT FROM f.current_version_id
                      AND superseded.id IS DISTINCT FROM f.replacing_version_id
                ) AS has_superseded
            FROM files f
            {CURRENT_JOIN_SQL}
            LEFT JOIN cleanup_batches cleanup ON cleanup.file_id = f.id
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
            current_id = row["current_version_id"]
            cloud_exists = current_id is not None and row["availability"] != "missing"
            protection = project_file(row, unconfirmed=self.protection_unconfirmed)
            upload_eligible = local_copy_is_upload_eligible(row)
            recoverable = current_is_recoverable(row)
            if name_from_path:
                name = row["path"]
            else:
                name = row["path"][len(prefix):] if prefix else row["path"]
            items.append(
                {
                    "type": "file",
                    "id": str(row["file_id"]),
                    "name": name,
                    "path": row["path"],
                    "local_exists": int(local_exists),
                    "local_size": row["local_size"] if local_exists else None,
                    "modified_at": (
                        datetime.fromtimestamp(int(row["local_mtime_ns"]) / 1e9, timezone.utc).isoformat()
                        if row["local_mtime_ns"] is not None else None
                    ),
                    "local_file_type": row["local_file_type"],
                    "cloud_exists": int(cloud_exists),
                    "cloud_size": row["cloud_size"],
                    "storage_class": row["storage_class"],
                    "integrity": row["integrity"],
                    "availability": row["availability"],
                    "current_version_id": _id(current_id),
                    "pending_version_id": _id(row["replacing_version_id"]),
                    "recoverable": recoverable,
                    "upload_verification_requested": row["upload_verification_requested"],
                    "verification_method": row["verification_method"],
                    "verification_fallback_reason": row["verification_fallback"],
                    "verification_egress_bytes": row["verification_egress_bytes"],
                    **protection,
                    "cloud_only_conflict": row["held_version_id"] is not None,
                    "reconciliation_block": row["reconciliation_block"],
                    "cleanup": _cleanup_state(row),
                    "upload_eligible": upload_eligible and not self.protection_unconfirmed,
                    "recover_eligible": not local_exists and recoverable,
                    "cleanup_eligible": (
                        local_copy_matches_current(row) and recoverable
                        and protection["state"] == "protected"
                    ),
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
            FROM files f
            {CURRENT_JOIN_SQL}
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
            ORDER BY lower(f.path) ASC, f.path ASC
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
            FROM files f
            {CURRENT_JOIN_SQL}
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
            ORDER BY lower(f.path) ASC, f.path ASC
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

    # -- Jobs: claims, leases and the fair scheduler (#589) ---------------------
    #
    # A claim is one row in ``job_leases`` (token, expiry), never a column of
    # the Job: renewal is a HOT update of that row and nothing else. The lease
    # token changes only while its Job row is locked (claim, takeover,
    # release, end), so a worker that locks the Job row and then reads its
    # lease sees a fresh answer; cancellation, takeover and expiry are always
    # checked on that live state (ADR-0035), never on a cached generation.

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
        run_after = f"{prefix}run_after"
        updated_at = f"{prefix}updated_at"
        clauses = [
            f"{status}='queued'",
            f"({status}='retrying' AND ({run_after} IS NULL OR {run_after} <= %s::timestamptz))",
            (
                f"({action}='cloud-purge' AND {status}='pending_delay' "
                f"AND {run_after} <= %s::timestamptz)"
            ),
        ]
        params: list[Any] = [now, now]
        if include_restoring:
            clauses.append(
                f"({action} IN ('recover', 'storage-class') "
                f"AND {status}='restoring' AND {updated_at} <= %s::timestamptz)"
            )
            params.append(restore_due_before)
        return "(" + " OR ".join(clauses) + ")", params

    @staticmethod
    def _stop_protection_purge_sql(prefix: str) -> str:
        return f"""(
            {prefix}action = 'cloud-purge' AND EXISTS (
                SELECT 1 FROM cloud_purge_requests stop_request
                WHERE stop_request.group_id = {prefix}group_id
                  AND stop_request.stop_protection
            )
        )"""

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
        acquire. An active Vault runs its ordinary work; a stopping Vault runs
        only the purge consented by Stop Protection; a paused Vault holds every
        new cloud Job except recoveries and a storage-class change already
        restoring (ADR-0033). An automatic upload not yet materialized waits
        while the Vault's automatic upload is off (#423). A live lease,
        anywhere in a purge group, keeps the Job unclaimable.
        """
        runnable_sql, params = cls._claimable_job_status_sql(
            prefix=prefix,
            now=now,
            restore_due_before=restore_due_before,
            include_restoring=True,
        )
        stop_purge = cls._stop_protection_purge_sql(prefix)
        predicate = f"""
            {runnable_sql}
            AND EXISTS (
                SELECT 1 FROM vaults scope_vault
                WHERE scope_vault.id = {prefix}vault_id
                  AND (
                        (scope_vault.lifecycle = 'active' AND NOT {stop_purge})
                     OR (scope_vault.lifecycle = 'stopping' AND {stop_purge})
                  )
                  AND (
                        NOT scope_vault.paused
                     OR {prefix}action = 'recover'
                     OR ({prefix}action = 'storage-class' AND {prefix}status = 'restoring')
                  )
            )
            AND (
                    {prefix}action <> 'upload'
                 OR {prefix}origin <> 'automatic'
                 OR {prefix}archive_version_id IS NOT NULL
                 OR COALESCE((
                        SELECT upload_policy.auto_upload FROM vault_operation_policies upload_policy
                        WHERE upload_policy.vault_id = {prefix}vault_id
                    ), TRUE)
            )
            AND NOT EXISTS (
                SELECT 1 FROM job_leases live_lease
                WHERE live_lease.job_id = {prefix}id
                  AND live_lease.expires_at > %s::timestamptz
            )
            AND (
                    {prefix}action <> 'cloud-purge'
                 OR NOT EXISTS (
                        SELECT 1 FROM jobs purge_peer
                        WHERE purge_peer.vault_id = {prefix}vault_id
                          AND purge_peer.group_id = {prefix}group_id
                          AND purge_peer.action = 'cloud-purge'
                          AND purge_peer.status NOT IN ('completed', 'failed', 'cancelled')
                          AND (
                                (
                                    purge_peer.status <> 'queued'
                                AND NOT (
                                    purge_peer.status = 'pending_delay'
                                    AND purge_peer.run_after <= %s::timestamptz
                                )
                                )
                             OR EXISTS (
                                    SELECT 1 FROM job_leases peer_lease
                                    WHERE peer_lease.job_id = purge_peer.id
                                      AND peer_lease.expires_at > %s::timestamptz
                                )
                          )
                    )
            )
        """
        return predicate, [*params, now, now, now]

    # SQL twin of ``job_scheduler.job_priority`` (alias ``j``), the order inside
    # a Vault: 0 Owner operations, 1 uploads the Owner asked for, 2 anything
    # admitted automatically. Candidate windows are cut per Vault and per tier.
    _SCHEDULING_TIER_SQL = (
        "CASE WHEN j.origin='automatic' THEN 2 WHEN j.action<>'upload' THEN 0 ELSE 1 END"
    )
    # SQL twin of ``job_scheduler.cross_vault_tier`` (ADR-0037): across Vaults
    # Owner uploads and automatic work share tier 1.
    _CROSS_VAULT_TIER_SQL = (
        "CASE WHEN j.origin<>'automatic' AND j.action<>'upload' THEN 0 ELSE 1 END"
    )

    @staticmethod
    def _lease_token(claim_token: Any) -> str:
        """A lease token is a UUID; anything else is a programming error."""
        if not claim_token:
            raise ValueError("A Job claim token is required")
        return str(uuid.UUID(str(claim_token)))

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
        #493, #500). Windows follow ``job_scheduler.job_priority``; rows are
        ordered by ``job_scheduler.cross_vault_tier`` (ADR-0037).
        ``exclude_job_ids`` removes candidates the worker already found not
        runnable *before* the window is cut. ``limit`` defaults the per-Vault
        window, not a global cut: every eligible Vault must be visible to the
        turn selector, including its automatic work.
        Each row carries the File's current path and its Vault's root and bucket.
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
                SELECT 1 FROM jobs serving
                JOIN job_leases serving_lease ON serving_lease.job_id = serving.id
                WHERE serving.vault_id = v.id
                  AND serving_lease.expires_at > %s::timestamptz
            )
        """ if unserved_only else ""
        return self.connection.execute(
            f"""
            SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid,
                   v.name AS vault_name, v.lifecycle AS vault_lifecycle,
                   t.bucket AS s3_bucket
            FROM vaults v
            JOIN vault_targets t ON t.id = v.target_id
            CROSS JOIN LATERAL (
                SELECT ranked.* FROM (
                    SELECT j.*,
                           row_number() OVER (
                               PARTITION BY {tier}
                               ORDER BY j.requested_at ASC, j.id ASC
                           ) AS tier_rank
                    FROM jobs j
                    WHERE j.vault_id = v.id
                      AND ({predicate})
                      AND j.id <> ALL(%s::bigint[])
                ) ranked
                WHERE ranked.tier_rank <= {window}
            ) j
            JOIN files f ON f.id = j.file_id
            WHERE v.lifecycle IN ('active', 'stopping') {idle_filter}
            ORDER BY {self._CROSS_VAULT_TIER_SQL} ASC, {tier} ASC,
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
        """Jobs currently holding a live lease, per Vault (scheduler fairness).

        A terminal Job holds no lease (the schema releases it), so a live lease
        is running work by construction.
        """
        rows = self.connection.execute(
            """
            SELECT j.vault_id, count(*) AS total
            FROM job_leases l JOIN jobs j ON j.id = l.job_id
            WHERE l.expires_at > %s::timestamptz
            GROUP BY j.vault_id
            """,
            (now,),
        ).fetchall()
        return {int(row["vault_id"]): int(row["total"]) for row in rows}

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

        Vault (shared, skip if contended), then the scheduler turn, then the
        Job row (skip if locked); the predicate is rechecked in a fresh
        statement under those locks. An expired lease is taken over only if it
        is still expired on its latest row version: a renewal that won keeps it.
        A loser observes no returned row instead of executing a stale
        in-memory selection.
        """
        token = self._lease_token(claim_token)
        if not self._lock_claim_vault(job_id):
            return None
        if not self._lock_scheduler_turn(job_id, scheduler_generation):
            return None
        if self.connection.execute(
            'SELECT id FROM jobs WHERE id=%s FOR UPDATE SKIP LOCKED', (job_id,),
        ).fetchone() is None:
            return None
        claimable_sql, claimable_params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        claimed = self.connection.execute(
            f"""
            WITH target AS (
                SELECT j.id, j.status, j.updated_at FROM jobs j
                WHERE j.id=%s AND j.action<>'cloud-purge' AND ({claimable_sql})
            ), lease AS (
                INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                SELECT id, %s, %s, %s FROM target
                ON CONFLICT (job_id) DO UPDATE
                SET token=EXCLUDED.token, claimed_at=EXCLUDED.claimed_at,
                    expires_at=EXCLUDED.expires_at
                WHERE job_leases.expires_at <= %s::timestamptz
                RETURNING job_id, token, claimed_at, expires_at
            )
            SELECT target.id, target.status, target.updated_at,
                   lease.token::text AS claim_token, lease.claimed_at,
                   lease.expires_at AS claim_expires_at
            FROM target JOIN lease ON lease.job_id = target.id
            """,
            (job_id, *claimable_params, token, claimed_at, claim_expires_at, now),
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
        destructive group between themselves: the group claims all its members
        or none, including under partial row contention.
        """
        token = self._lease_token(claim_token)
        if not self._lock_claim_vault(lead_job_id):
            return []
        # Resolve the group before taking Job locks. The advisory fence below
        # serializes different lead nominations for the same destructive group.
        lead = self.connection.execute(
            f"""
            SELECT j.vault_id, j.group_id, v.paused, v.lifecycle,
                   {self._stop_protection_purge_sql('j.')} AS stop_protection
            FROM jobs j JOIN vaults v ON v.id = j.vault_id
            WHERE j.id=%s AND j.action='cloud-purge'
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
        lifecycle_admits = (
            (lead["lifecycle"] == "active" and not lead["stop_protection"])
            or (lead["lifecycle"] == "stopping" and lead["stop_protection"])
        )
        if lead["paused"] or not lifecycle_admits:
            return []
        if not self._lock_scheduler_turn(lead_job_id, scheduler_generation):
            return []
        expected = self.connection.execute(
            "SELECT id FROM jobs WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'",
            (lead['vault_id'], lead['group_id']),
        ).fetchall()
        rows = self.connection.execute(
            """
            SELECT j.id, j.status,
                   (j.status = 'queued' OR (
                        j.status = 'pending_delay' AND j.run_after <= %s::timestamptz
                   )) AS runnable,
                   NOT EXISTS (
                       SELECT 1 FROM job_leases live_lease
                       WHERE live_lease.job_id = j.id
                         AND live_lease.expires_at > %s::timestamptz
                   ) AS available
            FROM jobs j
            WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
            ORDER BY j.requested_at ASC, j.id ASC FOR UPDATE SKIP LOCKED
            """,
            (now, now, lead["vault_id"], lead["group_id"]),
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
            not row["available"] or not row["runnable"] for row in active
        ):
            return []

        ids = [int(row["id"]) for row in active]
        self.connection.execute("SAVEPOINT purge_group_claim")
        leased = self.connection.execute(
            """
            INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
            SELECT member, %s, %s, %s FROM unnest(%s::bigint[]) AS member
            ON CONFLICT (job_id) DO UPDATE
            SET token=EXCLUDED.token, claimed_at=EXCLUDED.claimed_at,
                expires_at=EXCLUDED.expires_at
            WHERE job_leases.expires_at <= %s::timestamptz
            RETURNING job_id
            """,
            (token, claimed_at, claim_expires_at, ids, now),
        ).fetchall()
        if len(leased) != len(ids):
            # A previous holder renewed one member meanwhile: all or nothing.
            self.connection.execute("ROLLBACK TO SAVEPOINT purge_group_claim")
            return []
        self.connection.execute("RELEASE SAVEPOINT purge_group_claim")
        self.connection.execute(
            """
            UPDATE jobs
            SET status='cleaning', message=%s, message_key=%s,
                message_params=NULL, updated_at=%s
            WHERE id = ANY(%s::bigint[])
            """,
            (message, message_key, claimed_at, ids),
        )
        self._consume_scheduler_turn(lead_job_id, now=claimed_at)
        # Keep the scheduler's deterministic ordering for the selected lead.
        return self.connection.execute(
            """
            SELECT j.id, j.file_id, j.status, j.requested_at,
                   l.token::text AS claim_token, l.claimed_at,
                   l.expires_at AS claim_expires_at
            FROM jobs j JOIN job_leases l ON l.job_id = j.id
            WHERE j.id = ANY(%s::bigint[])
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            (ids,),
        ).fetchall()

    def renew_job_claim(
        self,
        *,
        job_id: int,
        claim_token: str,
        now: str,
        claim_expires_at: str,
        lock_job: bool = False,
    ) -> dict[str, Any] | None:
        """Extend a live claim, returning no row after cancellation, takeover or expiry.

        Only the lease row is rewritten (a HOT update). With ``lock_job`` the
        Job row is locked first, in the claim's lock order (Job, then lease):
        until the surrounding transaction commits no cancellation or takeover
        can win, and one that already won is seen here.
        """
        token = self._lease_token(claim_token)
        if lock_job:
            return self.connection.execute(
                """
                WITH job AS (
                    SELECT id, status, updated_at FROM jobs
                    WHERE id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
                    FOR NO KEY UPDATE
                )
                UPDATE job_leases l SET expires_at=%s
                FROM job
                WHERE l.job_id = job.id AND l.token=%s
                  AND l.expires_at > %s::timestamptz
                RETURNING job.id, job.status, l.token::text AS claim_token,
                          l.claimed_at, l.expires_at AS claim_expires_at,
                          job.updated_at
                """,
                (job_id, claim_expires_at, token, now),
            ).fetchone()
        return self.connection.execute(
            """
            UPDATE job_leases l SET expires_at=%s
            FROM jobs j
            WHERE l.job_id=%s AND j.id = l.job_id AND l.token=%s
              AND l.expires_at > %s::timestamptz
            RETURNING j.id, j.status, l.token::text AS claim_token,
                      l.claimed_at, l.expires_at AS claim_expires_at, j.updated_at
            """,
            (claim_expires_at, job_id, token, now),
        ).fetchone()

    def job_claim_is_live(self, *, job_id: int, claim_token: str, now: str) -> bool:
        """Read the exact predicate of :meth:`renew_job_claim` without writing.

        The answer is the same point-in-time fact a renewal returns. It extends
        nothing, so the caller must have renewed the lease recently.
        """
        return self.connection.execute(
            """
            SELECT 1 FROM job_leases
            WHERE job_id=%s AND token=%s AND expires_at > %s::timestamptz
            """,
            (job_id, self._lease_token(claim_token), now),
        ).fetchone() is not None

    def transition_job(
        self,
        *,
        job_id: int,
        status: str,
        message: str,
        message_key: str | None,
        message_params: Any,
        now: str,
        claim_token: str | None = None,
        claim_expires_at: str | None = None,
        transferred_bytes: int | None = None,
        run_after: str | None = None,
        retry_count: int | None = None,
        lease_renewed: bool = False,
    ) -> bool:
        """Move a Job to ``status``, compare-and-swap on its live lease.

        ``lease_renewed``: this transaction already renewed the lease under the
        Job row lock (a claim-fenced unit), so a held status is not renewed twice.

        With a claim the Job row is locked first and the lease checked fresh:
        a held status extends the lease, a waiting status gives it back, a
        terminal status ends the Job (the schema drops the lease). A
        cancellation, takeover or expiry that won makes this a no-op. Without
        a claim (administrative callers) only a non-terminal Job moves.
        A terminal Job never moves again.
        """
        self.transitioned_job = None
        if claim_token:
            token = self._lease_token(claim_token)
            if status in JOB_LEASE_HELD_STATUSES:
                if not claim_expires_at:
                    raise ValueError("A held Job needs its lease expiry")
                if lease_renewed:
                    pass
                elif self.renew_job_claim(
                    job_id=job_id, claim_token=token, now=now,
                    claim_expires_at=claim_expires_at, lock_job=True,
                ) is None:
                    return False
            elif self.connection.execute(
                """
                WITH job AS (
                    SELECT id FROM jobs
                    WHERE id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
                    FOR NO KEY UPDATE
                )
                DELETE FROM job_leases l USING job
                WHERE l.job_id = job.id AND l.token=%s
                  AND l.expires_at > %s::timestamptz
                RETURNING l.job_id
                """,
                (job_id, token, now),
            ).fetchone() is None:
                return False
        params = message_params
        if isinstance(params, Mapping):
            params = json.dumps(dict(params))
        result = self.connection.execute(
            """
            UPDATE jobs
            SET status=%s, message=%s, message_key=%s, message_params=%s::jsonb,
                updated_at=%s,
                transferred_bytes=COALESCE(%s, transferred_bytes),
                run_after=COALESCE(%s::timestamptz, run_after),
                retry_count=COALESCE(%s, retry_count)
            WHERE id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
            RETURNING id, file_id, vault_id, action, status, retry_count, run_after,
                      archive_version_id
            """,
            (status, message, message_key, params, now,
             None if transferred_bytes is None else max(0, int(transferred_bytes)),
             run_after, retry_count, job_id),
        )
        # The moved row, for the caller's same-transaction follow-ups (#597).
        self.transitioned_job = result.fetchone()
        return self.transitioned_job is not None

    def cancel_jobs(
        self,
        *,
        vault_id: int,
        job_ids: Sequence[int],
        message: str,
        message_key: str | None,
        now: str,
    ) -> list[int]:
        """Cancel non-terminal Jobs of one Vault; the caller holds the Vault lock.

        Cancellation wins on live state: it locks each Job row and ends it,
        which drops any lease (schema trigger), so the next renewal, progress
        or status write of a worker holding that lease finds nothing to update.
        """
        rows = self.connection.execute(
            """
            UPDATE jobs
            SET status='cancelled', message=%s, message_key=%s,
                message_params='{}'::jsonb, updated_at=%s
            WHERE vault_id=%s AND id = ANY(%s::bigint[])
              AND status NOT IN ('completed', 'failed', 'cancelled')
            RETURNING id
            """,
            (message, message_key, now, vault_id, [int(job_id) for job_id in job_ids]),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def record_job_progress(
        self,
        *,
        job_id: int,
        transferred_bytes: int,
        now: str,
        claim_token: str | None = None,
        claim_expires_at: str | None = None,
    ) -> bool:
        """Persist progress; with a claim, only while the lease is live (and extend it)."""
        if claim_token:
            if self.renew_job_claim(
                job_id=job_id, claim_token=claim_token, now=now,
                claim_expires_at=claim_expires_at or now, lock_job=True,
            ) is None:
                return False
        result = self.connection.execute(
            """
            UPDATE jobs SET transferred_bytes=%s, updated_at=%s
            WHERE id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (max(0, int(transferred_bytes)), now, job_id),
        )
        return result.rowcount == 1

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
        """Hold every member of a claimed purge group through one DB step.

        The members' Job rows are locked first, in group order, then their
        leases: a cancellation of the group either wins before this point or
        waits until the surrounding transaction commits.
        """
        token = self._lease_token(claim_token)
        self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
              AND status='cleaning'
            ORDER BY requested_at ASC, id ASC
            FOR NO KEY UPDATE
            """,
            (vault_id, group_id),
        ).fetchall()
        rows = self.connection.execute(
            """
            UPDATE job_leases l SET expires_at=%s
            FROM jobs j
            WHERE j.id = l.job_id
              AND j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND j.status='cleaning'
              AND l.token=%s
              AND l.expires_at > %s::timestamptz
            RETURNING l.job_id
            """,
            (claim_expires_at, vault_id, group_id, token, now),
        ).fetchall()
        return {int(row["job_id"]) for row in rows} == {
            int(job_id) for job_id in expected_job_ids
        }

    def release_purge_group_claim(
        self, *, vault_id: int, group_id: str, claim_token: str, now: str,
    ) -> list[int]:
        """Give a claimed purge group back before any provider deletion.

        Only members still leased by ``claim_token`` return to ``queued``; the
        group's consent (``cloud_purge_requests``) and deadline stay intact.
        """
        token = self._lease_token(claim_token)
        self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
              AND status='cleaning'
            ORDER BY requested_at ASC, id ASC
            FOR NO KEY UPDATE
            """,
            (vault_id, group_id),
        ).fetchall()
        released = self.connection.execute(
            """
            DELETE FROM job_leases l USING jobs j
            WHERE j.id = l.job_id
              AND j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND j.status='cleaning'
              AND l.token=%s AND l.expires_at > %s::timestamptz
            RETURNING l.job_id
            """,
            (vault_id, group_id, token, now),
        ).fetchall()
        ids = [int(row["job_id"]) for row in released]
        if ids:
            self.connection.execute(
                """
                UPDATE jobs SET status='queued', updated_at=%s
                WHERE id = ANY(%s::bigint[])
                """,
                (now, ids),
            )
        return ids

    def settle_stale_job(
        self,
        *,
        job_id: int,
        status: str,
        message: str,
        now: str,
        progress: str | None = None,
    ) -> bool:
        """Publish one restart decision only if no worker holds a live lease.

        The Job row is locked first; an expired lease is then deleted under
        its own row lock, so a renewal that won keeps the Job. Any lease still
        present afterwards is live and the decision is dropped.
        """
        if progress not in {None, "reset", "complete"}:
            raise ValueError("Unknown progress update")
        if self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
            FOR NO KEY UPDATE
            """,
            (job_id,),
        ).fetchone() is None:
            return False
        self.connection.execute(
            "DELETE FROM job_leases WHERE job_id=%s AND expires_at <= %s::timestamptz",
            (job_id, now),
        )
        if self.connection.execute(
            "SELECT 1 FROM job_leases WHERE job_id=%s", (job_id,),
        ).fetchone() is not None:
            return False
        progress_sql = {
            None: "",
            "reset": ", transferred_bytes=0",
            "complete": ", transferred_bytes=total_bytes",
        }[progress]
        result = self.connection.execute(
            f"""
            UPDATE jobs SET status=%s, message=%s, message_key=NULL,
                message_params=NULL, updated_at=%s{progress_sql}
            WHERE id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (status, message, now, job_id),
        )
        return result.rowcount == 1

    def load_claimed_purge_group(
        self,
        *,
        lead_job_id: int,
        claim_token: str,
        now: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load a complete, still-live cloud-purge claim and its consented targets.

        One item per member Job: its file, the exact Current it consented to
        (``archive_version_id``) and that version's key and VersionId.
        """
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
            SELECT j.id, j.status, j.file_id,
                   l.token::text AS claim_token, l.expires_at AS claim_expires_at,
                   COALESCE(
                       j.status = 'cleaning' AND l.token = %s
                       AND l.expires_at > %s::timestamptz, FALSE
                   ) AS owned
            FROM jobs j
            LEFT JOIN job_leases l ON l.job_id = j.id
            WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
              AND j.status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            (self._lease_token(claim_token), now, lead["vault_id"], lead["group_id"]),
        ).fetchall()
        if not all_active or not all(row["owned"] for row in all_active):
            return [], []
        items = self.connection.execute(
            """
            SELECT j.id, j.vault_id, j.file_id, j.group_id, j.archive_version_id,
                   j.origin, f.path, av.object_key, av.provider_version_id, av.size
            FROM jobs j
            JOIN files f ON f.id = j.file_id
            LEFT JOIN archive_versions av ON av.id = j.archive_version_id
            WHERE j.id = ANY(%s::bigint[])
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            ([int(row["id"]) for row in all_active],),
        ).fetchall()
        return all_active, items

    def claimable_queue_depth(
        self,
        *,
        now: str,
        restore_due_before: str,
        exclude_job_ids: Sequence[int] = (),
    ) -> int:
        """Count runnable, currently unleased Jobs.

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
            WHERE ({predicate})
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
                    CASE WHEN f.local_presence IN ('present', 'unsupported')
                         THEN coalesce(f.local_size, 0) ELSE 0 END AS local_bytes,
                    CASE WHEN cur.availability <> 'missing'
                         THEN coalesce(cur.size, 0) ELSE 0 END AS cloud_bytes
                FROM files f
                {CURRENT_JOIN_SQL}
                WHERE f.vault_id=%s AND {VISIBLE_SQL}
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
    def recovery_selection_scope(
        selection: Sequence[tuple[str, bool]], column: str = "f.path",
    ) -> tuple[str, list[Any]]:
        """One union of paths, never a concatenation that duplicates overlaps.

        Callers normalize paths first. The surrounding query must bind the
        Vault. starts_with treats SQL wildcard characters literally; the empty
        directory is the whole Vault.
        """
        clauses, params = [], []
        for path, is_directory in selection:
            if not is_directory:
                clauses.append(f"{column}=%s")
                params.append(path)
            elif not path:
                clauses.append("TRUE")
            else:
                clauses.append(f"starts_with({column}, %s)")
                params.append(path + "/")
        return "(" + " OR ".join(clauses) + ")", params

    @staticmethod
    def _queue_candidate_allowed(action: str, row: Mapping[str, Any]) -> bool:
        current = row["current_version_id"] is not None
        available = current and row["availability"] == "available"
        recoverable = current_is_recoverable(row)
        present_regular = (
            row["local_presence"] == "present" and row["local_file_type"] == "regular"
        )
        if action == "upload":
            # Cloud history is not protection. Admit a new Archive Version
            # when the present Local Copy is not matched to a verified,
            # available Current. Unchanged protected content stays
            # ineligible so duplicates are not queued.
            return local_copy_is_upload_eligible(row)
        if action == "recover":
            return row["local_presence"] != "present" and recoverable
        if action == "free-space":
            return present_regular and recoverable and local_copy_matches_current(row)
        if action == "rename":
            # The File already points at its new path. A rename Job migrates
            # verified cloud content to that path's key.
            return present_regular and recoverable
        if action == "storage-class":
            return available
        raise ValueError(f"Unsupported job action: {action}")

    def queue_jobs(
        self,
        *,
        vault_id: int,
        path: str,
        action: str,
        requested_at: str,
        group_id: str,
        is_directory: bool,
        archive_version_id: str | None = None,
        restore_tier: str | None = None,
        restore_days: int | None = None,
        estimated_cost_eur: float | None = None,
        estimated_hours: float | None = None,
        origin: str = "manual",
        target_storage_class: str | None = None,
        colder_only: bool = False,
        whole_vault: bool = False,
        recovery_selection: Sequence[tuple[str, bool]] | None = None,
        admission_file_ids: Sequence[str] | None = None,
        admission_verification_mode: str | None = None,
    ) -> tuple[list[int], int, int]:
        """Admit one Job per eligible File into one Job group.

        A File has at most one active operation (the schema's partial unique
        index); a File that already has one is skipped, never queued twice.
        Per-action details go to their own tables: upload verification to
        ``job_uploads``, restore tier/days to ``job_restores``, the target
        class to ``job_storage_class_changes``, the restore estimate to the
        group's ``restore_requests``. Returns (job ids, bytes, eligible count).
        """
        # This lock is deliberately first: take the Vault row lock before
        # the candidate read and inserts.
        # Every manual upload/recovery/free-space admission therefore observes
        # one serialized Vault state.
        lock_vault(self.connection, vault_id)
        self.last_skipped_same_class = 0
        if origin not in {"manual", "automatic"}:
            raise ValueError("Unsupported Job origin")
        if action not in {"upload", "recover", "free-space", "rename", "storage-class"}:
            raise ValueError(f"Unsupported job action: {action}")
        if admission_verification_mode not in {None, "s3_checksum", "read_back"}:
            raise ValueError("Invalid admission verification mode")
        clauses = [
            "f.vault_id=%s",
            """NOT EXISTS (
                SELECT 1 FROM jobs open_job
                WHERE open_job.file_id=f.id
                  AND open_job.status NOT IN ('completed', 'failed', 'cancelled')
            )""",
        ]
        params: list[Any] = [vault_id]
        if admission_file_ids is not None:
            if action != "upload":
                raise ValueError("Bounded admission is only for uploads")
            clauses.append("f.id=ANY(%s::uuid[])")
            params.append([str(file_id) for file_id in admission_file_ids])
        if recovery_selection:
            if action != "recover":
                raise ValueError("Selections are only supported for recovery")
            scope, scope_params = self.recovery_selection_scope(recovery_selection)
            clauses.append(scope)
            params.extend(scope_params)
        elif whole_vault:
            pass
        elif is_directory:
            scope, scope_params = self.recovery_selection_scope([(path, True)])
            clauses.append(scope)
            params.extend(scope_params)
        else:
            clauses.append("f.path=%s")
            params.append(path)
        if archive_version_id and action != "upload":
            # Exact IDs pin the current snapshot; they cannot select history.
            clauses.append("f.current_version_id=%s")
            params.append(archive_version_id)
        candidates = self.connection.execute(
            f"""
            SELECT
                f.id AS file_id,
                f.path,
                f.local_presence,
                f.local_file_type,
                f.local_size,
                f.local_sha256,
                f.matched_version_id,
                f.held_version_id,
                f.current_version_id,
                cur.size AS cloud_size,
                cur.plaintext_sha256 AS version_sha256,
                cur.integrity,
                cur.availability,
                cur.storage_class
            FROM files f
            LEFT JOIN archive_versions cur ON cur.id = f.current_version_id
            WHERE {" AND ".join(clauses)}
            ORDER BY f.path
            """,
            params,
        ).fetchall()
        eligible = []
        remediation_job_ids: list[int] = []
        skipped_same = 0
        target_class = (target_storage_class or "").upper() or None
        group_path = "" if whole_vault or recovery_selection else path
        # Only files that ever had an Upload Remediation episode go to its
        # arbiter: one indexed read, nothing per file in the normal case.
        with_episode: set[str] = set()
        if action == "upload" and candidates:
            from .services import upload_remediation
            with_episode = upload_remediation.episode_files(
                self.connection, [row["file_id"] for row in candidates])
        for row in candidates:
            allowed = self._queue_candidate_allowed(action, row)
            if allowed and str(row["file_id"]) in with_episode:
                handled, resumed = upload_remediation.intercept_upload(
                    self.connection, vault_id=vault_id, file_id=row["file_id"],
                    stamp=requested_at, automatic=origin == "automatic",
                    group_id=group_id, group_path=group_path,
                )
                if handled:
                    if resumed:
                        remediation_job_ids.append(int(resumed["job_id"]))
                        self._mark_path_aggregates_dirty(vault_id, row["path"])
                    allowed = False
            if allowed and action == "storage-class" and target_class:
                current = (row["storage_class"] or "STANDARD").upper()
                if current == target_class:
                    skipped_same += 1
                    allowed = False
            if allowed:
                eligible.append(row)
        self.last_skipped_same_class = skipped_same
        job_ids = list(remediation_job_ids)
        if not eligible:
            return job_ids, 0, len(remediation_job_ids)
        sizes = [
            int(
                (row["local_size"] if action == "upload" else row["cloud_size"])
                or 0
            )
            for row in eligible
        ]
        self.connection.execute(
            """
            INSERT INTO job_groups(id, vault_id, path, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (group_id, vault_id, group_path, requested_at),
        )
        # ON CONFLICT: a File that gained an active operation since the read
        # (one per File) is skipped, not failed.
        inserted = self.connection.execute(
            """
            INSERT INTO jobs(
                vault_id, file_id, archive_version_id, group_id, action,
                status, origin, total_bytes, requested_at, updated_at
            )
            SELECT %s, item.file_id, item.version_id, %s, %s,
                   'queued', %s, item.size, %s, %s
            FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[], %s::int[])
                 AS item(file_id, version_id, size, ord)
            ORDER BY item.ord
            ON CONFLICT DO NOTHING
            RETURNING id, file_id
            """,
            (
                vault_id, group_id, action, origin, requested_at, requested_at,
                [str(row["file_id"]) for row in eligible],
                [
                    None if action == "upload" else row["current_version_id"]
                    for row in eligible
                ],
                sizes,
                list(range(len(eligible))),
            ),
        ).fetchall()
        by_file = {str(row["file_id"]): int(row["id"]) for row in inserted}
        admitted = [
            (by_file[str(row["file_id"])], row, size)
            for row, size in zip(eligible, sizes)
            if str(row["file_id"]) in by_file
        ]
        new_ids = [job_id for job_id, _row, _size in admitted]
        if action == "upload" and new_ids:
            self.connection.execute(
                """
                INSERT INTO job_uploads(job_id, verification_mode)
                SELECT job_id, %s FROM unnest(%s::bigint[]) AS job_id
                """,
                (
                    admission_verification_mode
                    or self._upload_verification_mode(vault_id),
                    new_ids,
                ),
            )
            mark_paths_dirty(
                self.connection, vault_id,
                [row["path"] for _job_id, row, _size in admitted],
            )
        restoring = [
            job_id for job_id, row, _size in admitted
            if restore_tier and restore_days and self._carries_restore(action, row)
        ]
        if restoring:
            self.connection.execute(
                """
                INSERT INTO job_restores(job_id, action, tier, days)
                SELECT job_id, %s, %s, %s FROM unnest(%s::bigint[]) AS job_id
                """,
                (action, restore_tier, int(restore_days), restoring),
            )
        if action == "storage-class" and target_class and new_ids:
            self.connection.execute(
                """
                INSERT INTO job_storage_class_changes(job_id, target_storage_class, colder_only)
                SELECT job_id, %s, %s FROM unnest(%s::bigint[]) AS job_id
                """,
                (target_class, bool(colder_only), new_ids),
            )
        if (
            action in {"recover", "storage-class"}
            and new_ids
            and (estimated_cost_eur is not None or estimated_hours is not None)
        ):
            self.connection.execute(
                """
                INSERT INTO restore_requests(group_id, estimated_cost_eur, estimated_hours)
                VALUES (%s, %s, %s)
                ON CONFLICT (group_id) DO UPDATE
                SET estimated_cost_eur=EXCLUDED.estimated_cost_eur,
                    estimated_hours=EXCLUDED.estimated_hours
                """,
                (group_id, estimated_cost_eur, estimated_hours),
            )
        job_ids.extend(new_ids)
        total_bytes = sum(size for _job_id, _row, size in admitted)
        return job_ids, total_bytes, len(eligible) + len(remediation_job_ids)
