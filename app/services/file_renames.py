"""Renames of a Vault File: one identity, a new path (#803, ADR-0001).

A Vault File keeps its identity and its keeper (Current) when its Local Copy
moves. This Module owns that move in the catalog and nothing else implements it:

- the move itself (:meth:`FileRenames.rename_file`): Path History, the new
  path, the invalidated observation, the revoked deletion consent;
- the rename a scan can only infer (#621): the candidates
  (:meth:`FileRenames.list_rename_candidates`), their atomic confirmation
  (:meth:`FileRenames.confirm_file_rename`) and the SQL predicate of a new
  Local Copy whose digest must first say whether it is a renamed one
  (:func:`awaiting_rename_analysis_sql`).

What a rename then does in the cloud (the copy to the new key) belongs to the
worker; the observation of Local Copies stays with the catalog.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .catalog_shared import (
    VaultFileNotFound,
    as_timestamp,
    required_timestamp,
    revoke_deletion_consents,
    sha256_digest,
)
from .directory_aggregates import invalidate_for_confirmed_rename, mark_paths_dirty
from .job_queue import open_job_sql
from . import vault_locks

AUTO_RENAME_WINDOW = timedelta(hours=1)


def _within_rename_window(left: Any, right: Any) -> bool:
    start = as_timestamp(left)
    end = as_timestamp(right)
    if start is None or end is None:
        return False
    return abs(start - end) <= AUTO_RENAME_WINDOW


# A Local Copy a rename may have moved (#621): fingerprinted, regular, gone
# and not held. Exactly the predicate of files_rename_missing_size_idx (#759),
# so PostgreSQL answers each lookup from that small partial index.
def _rename_source_sql(alias: str) -> str:
    return f"""{alias}.local_presence='missing'
              AND {alias}.local_file_type='regular'
              AND {alias}.local_sha256 IS NOT NULL
              AND {alias}.held_version_id IS NULL"""


def awaiting_rename_analysis_sql(alias: str) -> str:
    """A new Local Copy that may be a Local Copy renamed without a scan (#621).

    It never had cloud content or a Job, has no fingerprint yet, and has the
    size of a fingerprinted Local Copy that went missing within the rename
    window. Its digest must decide whether it is that file before any
    automatic upload treats it as a new Vault File (ADR-0001).

    The fragment holds one ``%s`` placeholder, the Vault id of the statement,
    for the short-circuit (#759). The statement must already restrict
    ``{alias}.vault_id`` to that same id: across Vaults the short-circuit
    would be wrong without any error: an uncorrelated EXISTS that PostgreSQL
    evaluates once per statement, in the same snapshot as the rest. A Vault
    without any rename source skips the per-file analysis entirely; otherwise
    each lookup is a probe into ``files_rename_missing_size_idx``.
    """
    window = int(AUTO_RENAME_WINDOW.total_seconds())
    return f"""(
        {alias}.local_sha256 IS NULL
        AND {alias}.cloud_since IS NULL
        AND {alias}.held_version_id IS NULL
        AND EXISTS (
            SELECT 1 FROM files any_source
            WHERE any_source.vault_id=%s
              AND {_rename_source_sql("any_source")}
        )
        AND NOT EXISTS (SELECT 1 FROM jobs own WHERE own.file_id={alias}.id)
        AND EXISTS (
            SELECT 1 FROM files source
            WHERE source.vault_id={alias}.vault_id AND source.id<>{alias}.id
              AND {_rename_source_sql("source")}
              AND source.local_size={alias}.local_size
              AND source.local_observed_at
                  BETWEEN {alias}.local_observed_at - interval '{window} seconds'
                      AND {alias}.local_observed_at + interval '{window} seconds'
        )
    )"""


class FileRenames:
    """The renames of Vault Files, on one connection."""

    def __init__(self, connection: Any):
        self.connection = connection

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
        stamp = required_timestamp(changed_at)
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
        revoke_deletion_consents(
            self.connection, vault_id, [vault_file_id], stamp,
            reason="Cancelled: the file was renamed",
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
                    WHERE j.file_id = f.id AND {open_job_sql("j")}
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
            params.append(sha256_digest(digest))
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
        if vault_locks.lock_vault(
            self.connection, vault_id, vault_locks.NO_KEY_UPDATE, active_only=True,
        ) is None:
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
        stamp = required_timestamp(changed_at)
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
                      local_sha256, local_part_size, local_part_sha256, local_seen_at,
                      local_observed_at, local_content_observed_at
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
                local_mtime_ns=%s, local_sha256=%s, local_part_size=%s,
                local_part_sha256=%s, local_seen_at=%s,
                local_observed_at=%s, local_content_observed_at=%s,
                matched_version_id=NULL, held_version_id=NULL
            WHERE id=%s AND vault_id=%s AND generation=%s
            """,
            (
                new_path, stamp,
                local["local_presence"], local["local_file_type"], local["local_size"],
                local["local_mtime_ns"], local["local_sha256"], local["local_part_size"],
                local["local_part_sha256"], local["local_seen_at"],
                local["local_observed_at"], local["local_content_observed_at"],
                vault_file_id, vault_id, pair["missing_generation"],
            ),
        )
        self._require_one_rename_mutation(moved)
        revoke_deletion_consents(
            self.connection, vault_id, [vault_file_id], stamp,
            reason="Cancelled: the file was renamed",
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
