"""Local Copies of a Vault File: observation, fingerprint and pending timer (#803).

The ``files`` row of a Vault File records what was last observed at its path on
the Source. This Module owns that observation and nothing else writes it:

- a Local Copy seen, replaced or gone
  (:meth:`LocalCopies.observe_local_copy`,
  :meth:`LocalCopies.observe_local_copies_batch`,
  :meth:`LocalCopies.observe_replaced_local_copy`,
  :meth:`LocalCopies.mark_local_copy_missing`,
  :meth:`LocalCopies.mark_unseen_local_copies_missing`,
  :meth:`LocalCopies.mark_local_path_missing`);
- its fingerprint, published by compare-and-swap on the observation it was
  computed from (:meth:`LocalCopies.local_copies_to_fingerprint`,
  :meth:`LocalCopies.local_copies_to_identify`,
  :meth:`LocalCopies.publish_local_fingerprint`,
  :meth:`LocalCopies.retire_local_fingerprint`,
  :meth:`LocalCopies.publish_recovered_local_copy`);
- its explicit protection state: the pending timer and the reconciliation
  block (:meth:`LocalCopies.record_pending_files`,
  :meth:`LocalCopies.block_reconciliation`,
  :meth:`LocalCopies.clear_reconciliation_block`).

Archive Versions and Current belong to ``archive_versions``; renames to
``file_renames``.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Sequence

from psycopg.rows import tuple_row

from .catalog_shared import (
    as_timestamp,
    required_timestamp,
    revoke_deletion_consents,
    sha256_digest,
    vault_lock_touch,
)
from .directory_aggregates import (
    mark_directory_dirty,
    mark_path_dirty,
    mark_paths_dirty,
    request_vault_rebuild,
)
from .file_renames import awaiting_rename_analysis_sql
from .protection_states import PROTECTED_SQL


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


def _local_parts(
    parts: tuple[int, Sequence[bytes]] | None, size: int | None,
) -> tuple[int | None, list[bytes] | None]:
    """Validated part digests of a Local Copy fingerprint (#703), or none.

    Only for files of at least 8 MiB, cut with the Upload Snapshot's part
    size for that size: they are offered to the upload as its snapshot parts.
    """
    if parts is None:
        return None, None
    from .upload_snapshot import MULTIPART_THRESHOLD, part_count, upload_part_size
    part_size, digests = int(parts[0]), [bytes(value) for value in parts[1]]
    if (size is None or size < MULTIPART_THRESHOLD or part_size != upload_part_size(size)
            or len(digests) != part_count(size, part_size)
            or any(len(value) != 32 for value in digests)):
        raise ValueError("Local part digests do not describe this Local Copy")
    return part_size, digests


class LocalCopies:
    """The Local Copy columns of Vault Files, on the caller's transaction."""

    def __init__(self, connection: Any):
        self.connection = connection

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
        observed = [required_timestamp(entry[4]) for entry in ordered]
        seen = as_timestamp(seen_at)
        seen_values = [seen or stamp for stamp in observed]
        recovered = as_timestamp(recovered_at)
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
        # The fingerprint is unchanged when a missing Local Copy comes back
        # with the same type, size and mtime (#645): a Reconnect, a remount or
        # a file put back. Its digest is recomputed, but the content was not
        # observed to change, so its stability window, cleanup retention and
        # Stopped Upload hold keep their start.
        same_fingerprint_reappeared = """
            f.local_presence = 'missing'
            AND (f.local_file_type, f.local_size, f.local_mtime_ns)
                IS NOT DISTINCT FROM
                (EXCLUDED.local_file_type, EXCLUDED.local_size, EXCLUDED.local_mtime_ns)
        """
        vault_lock_touch(self.connection, vault_id)
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
                local_part_size = CASE WHEN {same_stat} THEN f.local_part_size END,
                local_part_sha256 = CASE WHEN {same_stat} THEN f.local_part_sha256 END,
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
                    WHEN ({content_changed} OR EXCLUDED.path = ANY(%s::text[]))
                     AND NOT ({same_fingerprint_reappeared})
                    THEN EXCLUDED.local_observed_at
                    ELSE COALESCE(f.local_content_observed_at, EXCLUDED.local_observed_at)
                END,
                local_recovered_at = COALESCE(EXCLUDED.local_recovered_at, f.local_recovered_at)
            RETURNING f.path, f.id
            """,
            (
                vault_id, recovered,
                paths, presences, types, sizes, mtimes, observed, seen_values,
                sorted(set(invalidate_paths)), sorted(set(invalidate_paths)),
            ),
        ).fetchall()
        file_ids = {str(row["path"]): str(row["id"]) for row in rows}
        invalidated = sorted({path for path in invalidate_paths if path in file_ids})
        if invalidated:
            # A content event forgets the digest even when size and mtime
            # were preserved; the upsert restarted its stability window.
            self.connection.execute(
                """
                UPDATE files
                SET local_sha256=NULL, local_part_size=NULL, local_part_sha256=NULL,
                    matched_version_id=NULL
                WHERE vault_id=%s AND path = ANY(%s::text[])
                  AND local_presence IS NOT NULL
                """,
                (vault_id, invalidated),
            )
        revoke_deletion_consents(
            self.connection, vault_id, list(file_ids.values()), max(observed),
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
            (required_timestamp(observed_at), vault_file_id),
        ).fetchone()
        if row is not None:
            mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])

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
        stamp = required_timestamp(observed_at)
        row = self.connection.execute(
            f"""
            UPDATE files f
            SET generation=f.generation + 1,
                local_presence=%s, local_file_type=%s, local_size=%s,
                local_mtime_ns=%s, local_sha256=NULL, local_part_size=NULL,
                local_part_sha256=NULL, matched_version_id=NULL,
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
            mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])

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
            (vault_id, vault_id),
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
        parts: tuple[int, Sequence[bytes]] | None = None,
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

        ``parts`` (#703) are the part size and part digests the same read
        computed for a file of at least 8 MiB; they are published in the same
        compare-and-swap. Without them the recorded parts stay only when the
        digest is the one already recorded (the same bytes, so the same
        parts), and are cleared otherwise: never parts of other content.
        """
        digest = sha256_digest(plaintext_sha256)
        part_size, part_digests = _local_parts(parts, size)
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
                local_part_size = CASE
                    WHEN %(has_parts)s THEN %(part_size)s::bigint
                    WHEN f.local_sha256 = %(digest)s THEN f.local_part_size
                END,
                local_part_sha256 = CASE
                    WHEN %(has_parts)s THEN %(part_sha256)s::bytea[]
                    WHEN f.local_sha256 = %(digest)s THEN f.local_part_sha256
                END,
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
                "has_parts": part_digests is not None,
                "part_size": part_size,
                "part_sha256": part_digests,
            },
        ).fetchone()
        if row is None:
            return False
        mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])
        return True

    def retire_local_fingerprint(
        self,
        vault_file_id: str,
        *,
        generation: int,
        plaintext_sha256: str | bytes,
        size: int,
        mtime_ns: int,
    ) -> bool:
        """Forget a fingerprint the Local Copy's bytes contradicted (#703).

        An upload that sent from the parts recorded with the fingerprint read
        other bytes: the content changed without changing size or mtime.
        Compare-and-swap on that observation and that digest: the digest, its
        parts and the match are cleared, so the Local Copy is verified again
        and the next upload hashes the whole file. A newer observation or
        digest is left alone (False).
        """
        row = self.connection.execute(
            """
            UPDATE files f
            SET local_sha256 = NULL, local_part_size = NULL, local_part_sha256 = NULL,
                matched_version_id = NULL
            WHERE f.id = %(id)s
              AND f.generation = %(generation)s
              AND f.local_presence = 'present'
              AND f.local_file_type = 'regular'
              AND f.local_size = %(size)s
              AND f.local_mtime_ns = %(mtime_ns)s
              AND f.local_sha256 = %(digest)s
            RETURNING f.vault_id, f.path
            """,
            {
                "id": vault_file_id, "generation": int(generation),
                "size": int(size), "mtime_ns": int(mtime_ns),
                "digest": sha256_digest(plaintext_sha256),
            },
        ).fetchone()
        if row is None:
            return False
        mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])
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
        digest = sha256_digest(plaintext_sha256)
        stamp = required_timestamp(recovered_at)
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
                local_sha256 = %(digest)s, local_part_size = NULL, local_part_sha256 = NULL,
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
        mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])
        return dict(row)

    def mark_unseen_local_copies_missing(
        self,
        *,
        vault_id: int,
        seen_at: Any,
        observed_at: Any,
        scan_started_at: Any | None = None,
        excluded_folders: Any = None,
    ) -> None:
        """Mark only rows not changed after this scan generation began.

        A watcher or another completed scan may legitimately update a Local
        Copy while the walk is in progress.  Such a newer observation must not
        be overwritten by the older scan's final missing transition. Rows in
        the ``excluded_folders`` the walk did not enter keep their last known
        state (ADR-0044).
        """
        started = as_timestamp(scan_started_at)
        outside, outside_params = (
            excluded_folders.not_inside_sql("path") if excluded_folders else ("TRUE", [])
        )
        self.connection.execute(
            f"""
            UPDATE files
            SET local_presence='missing', local_observed_at=%s,
                generation=generation + 1
            WHERE vault_id=%s
              AND local_presence IN ('present', 'unsupported')
              AND local_seen_at IS DISTINCT FROM %s
              AND (%s::timestamptz IS NULL OR local_observed_at <= %s::timestamptz)
              AND {outside}
            """,
            (
                required_timestamp(observed_at), vault_id,
                required_timestamp(seen_at), started, started, *outside_params,
            ),
        )
        request_vault_rebuild(self.connection, vault_id)

    def mark_local_path_missing(
        self, *, vault_id: int, path: str, observed_at: Any, excluded_folders: Any = None,
    ) -> None:
        """Mark the Local Copy at ``path``, or every one below it, missing.

        Rows inside ``excluded_folders`` keep their last known state (ADR-0044).
        """
        escaped = (
            path.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        outside, outside_params = (
            excluded_folders.not_inside_sql("path") if excluded_folders else ("TRUE", [])
        )
        self.connection.execute(
            f"""
            UPDATE files
            SET local_presence='missing', local_observed_at=%s,
                generation=generation + 1
            WHERE local_presence IS NOT NULL
              AND {outside}
              AND id IN (
                  SELECT id FROM files WHERE vault_id=%s AND path=%s
                  UNION ALL
                  SELECT id FROM files
                  WHERE vault_id=%s AND path LIKE %s ESCAPE '\\'
              )
            """,
            (
                required_timestamp(observed_at), *outside_params,
                vault_id, path, vault_id, f"{escaped}/%",
            ),
        )
        # Path may be a file or a directory prefix of many descendants.
        mark_path_dirty(self.connection, vault_id, path)
        mark_directory_dirty(self.connection, vault_id, path)

    # -- explicit protection state ------------------------------------------

    def record_pending_files(
        self, vault_id: int, pending: Mapping[str, Any]
    ) -> dict[str, datetime]:
        """Keep one continuous pending timer per pending file of the Vault.

        ``pending`` maps the id (text) of each file that is pending now to
        the instant its pending state began. Existing timers never reset; files no longer
        pending lose theirs. Returns the durable ``{file_id: pending_since}``
        of the files still pending at the write: a file protected after the
        caller read it (a concurrent upload published Current and cleared its
        timer) is left out and never gets its timer back (#549).
        """
        # One read of every timer of the Vault, after the caller's read: a
        # timer a concurrent upload cleared is gone here. The pending set is
        # compared in Python, never shipped to the server as one array per
        # timer row (#767). A write happens only for a timer that starts or
        # ends: an unchanged pending file is never rewritten (#622), so a long
        # queue costs no WAL per call.
        timers = self.connection.execute(
            f"""
            SELECT f.id::text, f.pending_since,
                   (f.local_presence = 'present' AND f.held_version_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM archive_versions cur
                        WHERE cur.id = f.current_version_id AND {PROTECTED_SQL}
                    )) AS eligible
            FROM files f
            WHERE f.vault_id = %s AND f.pending_since IS NOT NULL
            """,
            (vault_id,),
        )
        # Plain tuples: a long adoption has one timer row per pending file.
        timers.row_factory = tuple_row
        durable: dict[str, datetime] = {}
        timed: set[str] = set()
        ended: list[str] = []
        for file_id, since, eligible in timers.fetchall():
            timed.add(file_id)
            if file_id not in pending:
                ended.append(file_id)
            elif eligible:
                durable[file_id] = since
        if ended:
            self.connection.execute(
                """
                UPDATE files SET pending_since=NULL
                WHERE vault_id=%s AND id = ANY(%s::uuid[]) AND pending_since IS NOT NULL
                """,
                (vault_id, ended),
            )
        starting = [file_id for file_id in pending if file_id not in timed]
        if starting:
            sinces = [required_timestamp(pending[file_id]) for file_id in starting]
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
            (reason, required_timestamp(at), vault_file_id),
        )

    def clear_reconciliation_block(self, vault_file_id: str) -> None:
        self.connection.execute(
            """
            UPDATE files SET reconciliation_block=NULL, reconciliation_blocked_at=NULL
            WHERE id=%s AND reconciliation_block IS NOT NULL
            """,
            (vault_file_id,),
        )
