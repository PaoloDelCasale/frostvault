from __future__ import annotations

import uuid
from typing import Any, Sequence

from .audit import audit_log
from .services.vault_quotas import (
    TERMINAL_JOB_STATUSES,
    QuotaBlocked,
    QuotaEvaluation,
    admit_quota,
    lock_vault,
)
from .services.vault_recovery import require_upload_custody
from .services.lifecycle_pins import is_path_pinned
from .services.directory_aggregates import (
    count_child_directories,
    ensure_directory_aggregates,
    invalidate_for_confirmed_rename,
    list_child_directory_rows,
    mark_directory_dirty,
    mark_file_id_dirty,
    mark_path_dirty,
    request_vault_rebuild,
)


class VaultFileNotFound(LookupError):
    """A Vault File could not be resolved in the expected Vault."""


class ArchiveCatalog:
    """Keep versioned file invariants behind one persistence interface."""

    def __init__(self, connection: Any):
        self.connection = connection
        self.last_quota_evaluation = QuotaEvaluation(allowed=True)
        self.last_skipped_same_class = 0
        self.last_listing_rows_materialized = 0

    def _mark_path_aggregates_dirty(self, vault_id: int, path: str | None) -> None:
        mark_path_dirty(self.connection, vault_id, path)

    def _mark_file_aggregates_dirty(
        self, vault_id: int, vault_file_id: str | None
    ) -> None:
        mark_file_id_dirty(self.connection, vault_id, vault_file_id)

    def _request_aggregate_rebuild(self, vault_id: int) -> None:
        request_vault_rebuild(self.connection, vault_id)

    def _get_or_create_file(self, vault_id: int, path: str, created_at: str) -> str:
        # Serialize identity creation per Vault across scanner and worker transactions.
        self.connection.execute(
            "UPDATE vaults SET name=name WHERE id=%s",
            (vault_id,),
        )
        current = self.connection.execute(
            """
            SELECT vf.id
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE vf.vault_id=%s AND fp.path=%s
            """,
            (vault_id, path),
        ).fetchone()
        if current:
            return current["id"]
        file_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO vault_files(id, vault_id, status, created_at)
            VALUES (%s, %s, 'active', %s)
            """,
            (file_id, vault_id, created_at),
        )
        self.connection.execute(
            """
            INSERT INTO file_paths(
                vault_file_id, vault_id, path, valid_from, valid_to
            ) VALUES (%s, %s, %s, %s, NULL)
            """,
            (file_id, vault_id, path, created_at),
        )
        return file_id

    def _resolve_cloud_path_file(self, vault_id: int, path: str, created_at: str) -> str:
        """Map a cloud logical path to a Vault File without forking renamed identities.

        Prefers the current path, then an active Vault File that still carries
        ``path`` in Path History, and only then creates a new identity.
        """
        current = self.connection.execute(
            """
            SELECT vf.id
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE vf.vault_id=%s AND fp.path=%s
            """,
            (vault_id, path),
        ).fetchone()
        if current:
            return current["id"]
        historical = self.connection.execute(
            """
            SELECT vf.id
            FROM vault_files vf
            JOIN file_paths fp ON fp.vault_file_id=vf.id
            WHERE vf.vault_id=%s AND fp.path=%s AND vf.status='active'
            ORDER BY fp.valid_from DESC
            LIMIT 1
            """,
            (vault_id, path),
        ).fetchone()
        if historical:
            return historical["id"]
        return self._get_or_create_file(vault_id, path, created_at)

    def observe_local_copy(
        self,
        *,
        vault_id: int,
        path: str,
        file_type: str,
        size: int | None,
        mtime_ns: int | None,
        observed_at: str,
        seen_at: str | None = None,
    ) -> str:
        file_id = self._get_or_create_file(vault_id, path, observed_at)
        presence = "present" if file_type == "regular" else "unsupported"
        last_seen_at = seen_at or observed_at
        self.connection.execute(
            """
            INSERT INTO local_copies(
                vault_file_id, presence, file_type, size, mtime_ns,
                plaintext_sha256, matched_archive_version_id,
                last_seen_at, observed_at
            ) VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, %s)
            ON CONFLICT(vault_file_id) DO UPDATE SET
                presence=excluded.presence,
                file_type=excluded.file_type,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                plaintext_sha256=CASE
                    WHEN local_copies.file_type=excluded.file_type
                     AND (
                         local_copies.size=excluded.size
                         OR (local_copies.size IS NULL AND excluded.size IS NULL)
                     )
                     AND (
                         local_copies.mtime_ns=excluded.mtime_ns
                         OR (
                             local_copies.mtime_ns IS NULL
                             AND excluded.mtime_ns IS NULL
                         )
                     )
                    THEN local_copies.plaintext_sha256
                    ELSE NULL
                END,
                matched_archive_version_id=CASE
                    WHEN local_copies.file_type=excluded.file_type
                     AND (
                         local_copies.size=excluded.size
                         OR (local_copies.size IS NULL AND excluded.size IS NULL)
                     )
                     AND (
                         local_copies.mtime_ns=excluded.mtime_ns
                         OR (
                             local_copies.mtime_ns IS NULL
                             AND excluded.mtime_ns IS NULL
                         )
                     )
                    THEN local_copies.matched_archive_version_id
                    ELSE NULL
                END,
                last_seen_at=excluded.last_seen_at,
                observed_at=excluded.observed_at
            """,
            (
                file_id,
                presence,
                file_type,
                size,
                mtime_ns,
                last_seen_at,
                observed_at,
            ),
        )
        self._mark_path_aggregates_dirty(vault_id, path)
        return file_id

    def record_archive_version(
        self,
        *,
        vault_id: int,
        path: str,
        object_key: str,
        provider_version_id: str,
        size: int | None,
        storage_class: str | None,
        etag: str | None,
        uploaded_at: str,
        observed_at: str,
        scan_id: str,
        origin: str = "discovered",
        desired_policy_id: str | None = None,
        applied_policy_id: str | None = None,
    ) -> str:
        # Resolve existing Archive Version BEFORE minting a Vault File so a
        # cloud scan of a renamed file's old key cannot orphan a new identity.
        existing = self.connection.execute(
            """
            SELECT id FROM archive_versions
            WHERE vault_id=%s AND object_key=%s AND provider_version_id=%s
            """,
            (vault_id, object_key, provider_version_id),
        ).fetchone()
        if existing:
            self.connection.execute(
                """
                UPDATE archive_versions
                SET availability='available',
                    availability_checked_at=%s,
                    storage_class=%s,
                    etag=%s,
                    desired_policy_id=COALESCE(%s, desired_policy_id),
                    applied_policy_id=COALESCE(%s, applied_policy_id)
                WHERE id=%s
                """,
                (
                    scan_id,
                    storage_class,
                    etag,
                    desired_policy_id,
                    applied_policy_id,
                    existing["id"],
                ),
            )
            self._mark_path_aggregates_dirty(vault_id, path)
            return existing["id"]
        file_id = self._resolve_cloud_path_file(vault_id, path, observed_at)
        self.connection.execute(
            "UPDATE vault_files SET status=status WHERE id=%s",
            (file_id,),
        )
        latest = self.connection.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) AS version_number
            FROM archive_versions WHERE vault_file_id=%s
            """,
            (file_id,),
        ).fetchone()
        version_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO archive_versions(
                id, vault_file_id, vault_id, version_number, object_key,
                provider_version_id, size, storage_class, etag,
                provider_checksums, plaintext_sha256, uploaded_at,
                discovered_at, origin, integrity, verified_at,
                availability, availability_checked_at, restore_state,
                restore_expiry, restore_checked_at, desired_policy_id,
                applied_policy_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                NULL, NULL, %s, %s, %s, 'unverified', NULL,
                'available', %s, NULL, NULL, NULL, %s, %s
            )
            """,
            (
                version_id,
                file_id,
                vault_id,
                int(latest["version_number"]) + 1,
                object_key,
                provider_version_id,
                size,
                storage_class,
                etag,
                uploaded_at,
                observed_at,
                origin,
                scan_id,
                desired_policy_id,
                applied_policy_id,
            ),
        )
        self._mark_path_aggregates_dirty(vault_id, path)
        return version_id

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

    def get_job_target(self, job_id: int) -> dict[str, Any] | None:
        return self.connection.execute(
            """
            SELECT
                j.id AS job_id,
                j.vault_id,
                j.vault_file_id,
                j.archive_version_id,
                j.upload_plaintext_sha256,
                j.path,
                lc.presence AS local_presence,
                lc.file_type AS local_file_type,
                lc.size AS local_size,
                lc.mtime_ns AS local_mtime_ns,
                lc.plaintext_sha256 AS local_sha256,
                lc.matched_archive_version_id,
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

    def mark_local_copy_missing(
        self, vault_file_id: str, *, observed_at: str
    ) -> None:
        owned = self.connection.execute(
            """
            SELECT vault_id FROM vault_files WHERE id=%s
            """,
            (vault_file_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE local_copies
            SET presence='missing', observed_at=%s
            WHERE vault_file_id=%s
            """,
            (observed_at, vault_file_id),
        )
        if owned is not None:
            self._mark_file_aggregates_dirty(
                int(owned["vault_id"]), vault_file_id
            )

    def observe_replaced_local_copy(
        self,
        vault_file_id: str,
        *,
        file_type: str,
        size: int | None,
        mtime_ns: int | None,
        observed_at: str,
    ) -> None:
        presence = "present" if file_type == "regular" else "unsupported"
        owned = self.connection.execute(
            """
            SELECT vault_id FROM vault_files WHERE id=%s
            """,
            (vault_file_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE local_copies
            SET presence=%s, file_type=%s, size=%s, mtime_ns=%s,
                plaintext_sha256=NULL, matched_archive_version_id=NULL,
                last_seen_at=%s, observed_at=%s
            WHERE vault_file_id=%s
            """,
            (
                presence,
                file_type,
                size,
                mtime_ns,
                observed_at,
                observed_at,
                vault_file_id,
            ),
        )
        if owned is not None:
            self._mark_file_aggregates_dirty(
                int(owned["vault_id"]), vault_file_id
            )

    def mark_version_verified(
        self,
        archive_version_id: str,
        *,
        plaintext_sha256: str,
        verified_at: str,
    ) -> None:
        if len(plaintext_sha256) != 64:
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        int(plaintext_sha256, 16)
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
            SET plaintext_sha256=%s, integrity='verified', verified_at=%s
            WHERE id=%s
            """,
            (plaintext_sha256.lower(), verified_at, archive_version_id),
        )
        if owned is not None:
            self._mark_file_aggregates_dirty(
                int(owned["vault_id"]), owned["vault_file_id"]
            )

    def mark_version_mismatch(
        self,
        archive_version_id: str,
        *,
        plaintext_sha256: str,
        checked_at: str,
    ) -> None:
        if len(plaintext_sha256) != 64:
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        int(plaintext_sha256, 16)
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
            SET plaintext_sha256=%s, integrity='mismatch', verified_at=%s
            WHERE id=%s
            """,
            (plaintext_sha256.lower(), checked_at, archive_version_id),
        )
        if owned is not None:
            self._mark_file_aggregates_dirty(
                int(owned["vault_id"]), owned["vault_file_id"]
            )

    def set_local_fingerprint(
        self,
        *,
        vault_id: int,
        path: str,
        plaintext_sha256: str,
        matched_archive_version_id: str | None,
    ) -> None:
        if len(plaintext_sha256) != 64:
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        int(plaintext_sha256, 16)
        self.connection.execute(
            """
            UPDATE local_copies
            SET plaintext_sha256=%s, matched_archive_version_id=%s
            WHERE vault_file_id=(
                SELECT vf.id
                FROM vault_files vf
                JOIN file_paths fp
                  ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                WHERE vf.vault_id=%s AND fp.path=%s
            )
            """,
            (
                plaintext_sha256.lower(),
                matched_archive_version_id,
                vault_id,
                path,
            ),
        )
        self._mark_path_aggregates_dirty(vault_id, path)

    def rename_file(
        self,
        vault_file_id: str,
        *,
        new_path: str,
        changed_at: str,
        vault_id: int,
    ) -> None:
        current = self.connection.execute(
            """
            SELECT fp.path
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id
             AND fp.vault_id=vf.vault_id
             AND fp.valid_to IS NULL
            WHERE vf.id=%s AND vf.vault_id=%s AND vf.status='active'
            """,
            (vault_file_id, vault_id),
        ).fetchone()
        if current is None:
            raise VaultFileNotFound()
        if current["path"] == new_path:
            return
        # Both old and new ancestor chains must rebuild before/after the move.
        self._mark_path_aggregates_dirty(vault_id, current["path"])
        self._mark_path_aggregates_dirty(vault_id, new_path)
        self.connection.execute(
            """
            UPDATE file_paths SET valid_to=%s
            WHERE vault_file_id=%s AND vault_id=%s AND valid_to IS NULL
            """,
            (changed_at, vault_file_id, vault_id),
        )
        self.connection.execute(
            """
            INSERT INTO file_paths(
                vault_file_id, vault_id, path, valid_from, valid_to
            ) VALUES (%s, %s, %s, %s, NULL)
            """,
            (vault_file_id, vault_id, new_path, changed_at),
        )

    def list_rename_candidates(self, vault_id: int) -> list[dict[str, Any]]:
        """Match newly missing Local Copies to newly present paths by digest."""
        rows = self.connection.execute(
            """
            SELECT
                vf.id AS vault_file_id,
                fp.path,
                lc.presence,
                lc.plaintext_sha256 AS digest
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id
             AND fp.vault_id=vf.vault_id
             AND fp.valid_to IS NULL
            JOIN local_copies lc ON lc.vault_file_id=vf.id
            WHERE vf.vault_id=%s
              AND vf.status='active'
              AND lc.file_type='regular'
              AND lc.plaintext_sha256 IS NOT NULL
              AND lc.presence IN ('missing', 'present')
            ORDER BY lower(fp.path)
            """,
            (vault_id,),
        ).fetchall()
        missing_by_digest: dict[str, list[dict[str, Any]]] = {}
        present_by_digest: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            digest = str(row["digest"]).lower()
            bucket = (
                missing_by_digest
                if row["presence"] == "missing"
                else present_by_digest
            )
            bucket.setdefault(digest, []).append(row)
        candidates: list[dict[str, Any]] = []
        for digest, missing_rows in missing_by_digest.items():
            present_rows = present_by_digest.get(digest, [])
            if not present_rows:
                continue
            if len(missing_rows) == 1 and len(present_rows) == 1:
                decision = "auto"
            else:
                decision = "ambiguous"
            for missing in missing_rows:
                for present in present_rows:
                    candidates.append(
                        {
                            "missing_vault_file_id": missing["vault_file_id"],
                            "missing_path": missing["path"],
                            "new_vault_file_id": present["vault_file_id"],
                            "new_path": present["path"],
                            "digest": digest,
                            "decision": decision,
                        }
                    )
        return candidates

    def _rename_backend(self) -> str:
        return str(getattr(self.connection, "backend", "postgresql") or "postgresql")

    def _reserve_rename_confirmation(self, vault_id: int) -> None:
        """Serialize a confirmation before its candidate is inspected.

        SQLite must reserve its single writer before the candidate SELECT; a
        deferred transaction would let two confirmations read the same pair
        and make one fail later with ``database is locked``. PostgreSQL locks
        one deterministic Vault row before locking the two candidate rows.
        Scanner writes that use the same Vault lock serialize normally, while
        the conditional writes below still fail closed for any writer that
        does not take it.
        """
        if self._rename_backend() == "sqlite":
            raw_connection = getattr(self.connection, "connection", None)
            if raw_connection is None or not raw_connection.in_transaction:
                self.connection.begin_immediate()
            query = """
                SELECT id FROM vaults
                WHERE id=%s AND decommission_state='active'
            """
        else:
            query = """
                SELECT id FROM vaults
                WHERE id=%s AND decommission_state='active'
                FOR UPDATE
            """
        if self.connection.execute(query, (vault_id,)).fetchone() is None:
            raise VaultFileNotFound()

    @staticmethod
    def _snapshot_value_condition(
        column: str, value: Any, *, casefold: bool = False
    ) -> tuple[str, list[Any]]:
        if value is None:
            return f"{column} IS NULL", []
        if casefold:
            return f"lower({column})=lower(%s)", [value]
        return f"{column}=%s", [value]

    @classmethod
    def _local_copy_snapshot_conditions(
        cls,
        alias: str,
        snapshot: dict[str, Any],
        prefix: str,
    ) -> tuple[list[str], list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        for column in (
            "presence",
            "file_type",
            "size",
            "mtime_ns",
            "plaintext_sha256",
            "matched_archive_version_id",
            "last_seen_at",
            "observed_at",
        ):
            condition, values = cls._snapshot_value_condition(
                f"{alias}.{column}",
                snapshot[f"{prefix}_{column}"],
                casefold=column == "plaintext_sha256",
            )
            conditions.append(condition)
            params.extend(values)
        return conditions, params

    @classmethod
    def _file_snapshot_conditions(
        cls,
        alias: str,
        *,
        vault_file_id: str,
        vault_id: int,
        status: str,
        retired_at: str | None,
    ) -> tuple[list[str], list[Any]]:
        conditions = [f"{alias}.id=%s", f"{alias}.vault_id=%s"]
        params: list[Any] = [vault_file_id, vault_id]
        for column, value in (("status", status), ("retired_at", retired_at)):
            condition, values = cls._snapshot_value_condition(
                f"{alias}.{column}", value
            )
            conditions.append(condition)
            params.extend(values)
        return conditions, params

    @classmethod
    def _path_snapshot_conditions(
        cls,
        alias: str,
        *,
        vault_file_id: str,
        vault_id: int,
        state: tuple[str, str, str | None],
    ) -> tuple[list[str], list[Any]]:
        path, valid_from, valid_to = state
        conditions = [
            f"{alias}.vault_file_id=%s",
            f"{alias}.vault_id=%s",
            f"{alias}.path=%s",
            f"{alias}.valid_from=%s",
        ]
        params: list[Any] = [vault_file_id, vault_id, path, valid_from]
        valid_to_condition, valid_to_params = cls._snapshot_value_condition(
            f"{alias}.valid_to", valid_to
        )
        conditions.append(valid_to_condition)
        params.extend(valid_to_params)
        return conditions, params

    def _rename_pair_predicate(
        self,
        snapshot: dict[str, Any],
        *,
        missing_path: tuple[str, str, str | None],
        provisional_status: str,
        provisional_retired_at: str | None,
        provisional_path: tuple[str, str, str | None],
        provisional_copy_present: bool,
    ) -> tuple[str, list[Any]]:
        """Describe exactly the candidate state expected by one CAS mutation."""
        missing_file_conditions, missing_file_params = self._file_snapshot_conditions(
            "missing",
            vault_file_id=snapshot["missing_vault_file_id"],
            vault_id=snapshot["vault_id"],
            status=snapshot["missing_status"],
            retired_at=snapshot["missing_retired_at"],
        )
        missing_path_conditions, missing_path_params = self._path_snapshot_conditions(
            "missing_path",
            vault_file_id=snapshot["missing_vault_file_id"],
            vault_id=snapshot["vault_id"],
            state=missing_path,
        )
        missing_copy_conditions, missing_copy_params = (
            self._local_copy_snapshot_conditions("missing_copy", snapshot, "missing")
        )
        missing_where = " AND ".join(
            [
                *missing_file_conditions,
                *missing_path_conditions,
                *missing_copy_conditions,
            ]
        )
        missing_sql = f"""
            EXISTS (
                SELECT 1
                FROM vault_files missing
                JOIN file_paths missing_path
                  ON missing_path.vault_file_id=missing.id
                 AND missing_path.vault_id=missing.vault_id
                JOIN local_copies missing_copy
                  ON missing_copy.vault_file_id=missing.id
                WHERE {missing_where}
            )
        """

        provisional_file_conditions, provisional_file_params = (
            self._file_snapshot_conditions(
                "provisional",
                vault_file_id=snapshot["provisional_vault_file_id"],
                vault_id=snapshot["vault_id"],
                status=provisional_status,
                retired_at=provisional_retired_at,
            )
        )
        provisional_path_conditions, provisional_path_params = (
            self._path_snapshot_conditions(
                "provisional_path",
                vault_file_id=snapshot["provisional_vault_file_id"],
                vault_id=snapshot["vault_id"],
                state=provisional_path,
            )
        )
        provisional_joins = [
            "FROM vault_files provisional",
            "JOIN file_paths provisional_path",
            "  ON provisional_path.vault_file_id=provisional.id",
            " AND provisional_path.vault_id=provisional.vault_id",
        ]
        provisional_conditions = [
            *provisional_file_conditions,
            *provisional_path_conditions,
        ]
        provisional_params = [
            *provisional_file_params,
            *provisional_path_params,
        ]
        if provisional_copy_present:
            provisional_copy_conditions, provisional_copy_params = (
                self._local_copy_snapshot_conditions(
                    "provisional_copy", snapshot, "provisional"
                )
            )
            provisional_joins.extend(
                [
                    "JOIN local_copies provisional_copy",
                    "  ON provisional_copy.vault_file_id=provisional.id",
                ]
            )
            provisional_conditions.extend(provisional_copy_conditions)
            provisional_params.extend(provisional_copy_params)
        else:
            provisional_conditions.append(
                "NOT EXISTS ("
                "SELECT 1 FROM local_copies consumed_copy "
                "WHERE consumed_copy.vault_file_id=provisional.id"
                ")"
            )
        provisional_sql = f"""
            EXISTS (
                SELECT 1
                {' '.join(provisional_joins)}
                WHERE {' AND '.join(provisional_conditions)}
            )
        """
        return (
            f"({missing_sql}) AND ({provisional_sql})",
            [
                *missing_file_params,
                *missing_path_params,
                *missing_copy_params,
                *provisional_params,
            ],
        )

    def _require_one_rename_mutation(self, result: Any) -> None:
        """Fail closed when a conditional candidate mutation loses its CAS."""
        if getattr(result, "rowcount", None) != 1:
            self.connection.execute("ROLLBACK TO SAVEPOINT rename_confirmation_cas")
            self.connection.execute("RELEASE SAVEPOINT rename_confirmation_cas")
            raise VaultFileNotFound()

    def _load_rename_candidate(
        self,
        *,
        vault_file_id: str,
        new_path: str,
        vault_id: int,
    ) -> dict[str, Any] | None:
        query = """
            SELECT
                missing.id AS missing_vault_file_id,
                missing.status AS missing_status,
                missing.retired_at AS missing_retired_at,
                missing_path.path AS missing_path,
                missing_path.valid_from AS missing_path_valid_from,
                missing_copy.presence AS missing_presence,
                missing_copy.file_type AS missing_file_type,
                missing_copy.size AS missing_size,
                missing_copy.mtime_ns AS missing_mtime_ns,
                missing_copy.plaintext_sha256 AS missing_plaintext_sha256,
                missing_copy.matched_archive_version_id
                    AS missing_matched_archive_version_id,
                missing_copy.last_seen_at AS missing_last_seen_at,
                missing_copy.observed_at AS missing_observed_at,
                provisional.id AS provisional_vault_file_id,
                provisional.status AS provisional_status,
                provisional.retired_at AS provisional_retired_at,
                provisional_path.path AS provisional_path,
                provisional_path.valid_from AS provisional_path_valid_from,
                provisional_copy.presence AS provisional_presence,
                provisional_copy.file_type AS provisional_file_type,
                provisional_copy.size AS provisional_size,
                provisional_copy.mtime_ns AS provisional_mtime_ns,
                provisional_copy.plaintext_sha256 AS provisional_plaintext_sha256,
                provisional_copy.matched_archive_version_id
                    AS provisional_matched_archive_version_id,
                provisional_copy.last_seen_at AS provisional_last_seen_at,
                provisional_copy.observed_at AS provisional_observed_at
            FROM vault_files missing
            JOIN file_paths missing_path
              ON missing_path.vault_file_id=missing.id
             AND missing_path.vault_id=missing.vault_id
             AND missing_path.valid_to IS NULL
            JOIN local_copies missing_copy
              ON missing_copy.vault_file_id=missing.id
            JOIN vault_files provisional
              ON provisional.vault_id=missing.vault_id
             AND provisional.status='active'
            JOIN file_paths provisional_path
              ON provisional_path.vault_file_id=provisional.id
             AND provisional_path.vault_id=provisional.vault_id
             AND provisional_path.valid_to IS NULL
            JOIN local_copies provisional_copy
              ON provisional_copy.vault_file_id=provisional.id
            WHERE missing.id=%s
              AND missing.vault_id=%s
              AND missing.status='active'
              AND missing_copy.file_type='regular'
              AND missing_copy.presence='missing'
              AND missing_copy.plaintext_sha256 IS NOT NULL
              AND provisional.id<>missing.id
              AND provisional.vault_id=%s
              AND provisional_path.path=%s
              AND provisional_copy.file_type='regular'
              AND provisional_copy.presence='present'
              AND provisional_copy.plaintext_sha256 IS NOT NULL
              AND lower(provisional_copy.plaintext_sha256)
                  = lower(missing_copy.plaintext_sha256)
        """
        if self._rename_backend() != "sqlite":
            query += """
                FOR UPDATE OF
                    missing, missing_path, missing_copy,
                    provisional, provisional_path, provisional_copy
            """
        snapshot = self.connection.execute(
            query,
            (vault_file_id, vault_id, vault_id, new_path),
        ).fetchone()
        if snapshot is not None:
            snapshot["vault_id"] = vault_id
        return snapshot

    def confirm_file_rename(
        self,
        *,
        vault_file_id: str,
        new_path: str,
        changed_at: str,
        vault_id: int,
    ) -> str:
        """Atomically consume one current, Vault-local rename candidate.

        Every state transition is a conditional compare-and-swap against the
        full candidate snapshot. A competing confirmation or scanner update
        therefore changes a rowcount to zero and raises ``VaultFileNotFound``;
        the caller's transaction rolls back all earlier transitions.
        """
        self._reserve_rename_confirmation(vault_id)
        snapshot = self._load_rename_candidate(
            vault_file_id=vault_file_id,
            new_path=new_path,
            vault_id=vault_id,
        )
        if snapshot is None:
            raise VaultFileNotFound()
        # The endpoint maps VaultFileNotFound to its non-oracular 404 outside
        # the transaction. Keep this scoped rollback too, so direct catalog
        # callers cannot accidentally commit a partial candidate consumption.
        self.connection.execute("SAVEPOINT rename_confirmation_cas")

        missing_current = (
            snapshot["missing_path"],
            snapshot["missing_path_valid_from"],
            None,
        )
        missing_closed = (
            snapshot["missing_path"],
            snapshot["missing_path_valid_from"],
            changed_at,
        )
        missing_renamed = (new_path, changed_at, None)
        provisional_current = (
            snapshot["provisional_path"],
            snapshot["provisional_path_valid_from"],
            None,
        )
        provisional_closed = (
            snapshot["provisional_path"],
            snapshot["provisional_path_valid_from"],
            changed_at,
        )
        provisional_id = snapshot["provisional_vault_file_id"]

        active_pair, active_pair_params = self._rename_pair_predicate(
            snapshot,
            missing_path=missing_current,
            provisional_status="active",
            provisional_retired_at=snapshot["provisional_retired_at"],
            provisional_path=provisional_current,
            provisional_copy_present=True,
        )
        claimed = self.connection.execute(
            f"""
            UPDATE vault_files
            SET status='retired', retired_at=%s
            WHERE id=%s
              AND vault_id=%s
              AND status='active'
              AND ({active_pair})
            """,
            (changed_at, provisional_id, vault_id, *active_pair_params),
        )
        self._require_one_rename_mutation(claimed)

        claimed_current_pair, claimed_current_params = self._rename_pair_predicate(
            snapshot,
            missing_path=missing_current,
            provisional_status="retired",
            provisional_retired_at=changed_at,
            provisional_path=provisional_current,
            provisional_copy_present=True,
        )
        closed_provisional = self.connection.execute(
            f"""
            UPDATE file_paths
            SET valid_to=%s
            WHERE vault_file_id=%s
              AND vault_id=%s
              AND path=%s
              AND valid_from=%s
              AND valid_to IS NULL
              AND ({claimed_current_pair})
            """,
            (
                changed_at,
                provisional_id,
                vault_id,
                snapshot["provisional_path"],
                snapshot["provisional_path_valid_from"],
                *claimed_current_params,
            ),
        )
        self._require_one_rename_mutation(closed_provisional)

        claimed_closed_pair, claimed_closed_params = self._rename_pair_predicate(
            snapshot,
            missing_path=missing_current,
            provisional_status="retired",
            provisional_retired_at=changed_at,
            provisional_path=provisional_closed,
            provisional_copy_present=True,
        )
        consumed_copy = self.connection.execute(
            f"""
            DELETE FROM local_copies
            WHERE vault_file_id=%s
              AND ({claimed_closed_pair})
            """,
            (provisional_id, *claimed_closed_params),
        )
        self._require_one_rename_mutation(consumed_copy)

        consumed_pair, consumed_pair_params = self._rename_pair_predicate(
            snapshot,
            missing_path=missing_current,
            provisional_status="retired",
            provisional_retired_at=changed_at,
            provisional_path=provisional_closed,
            provisional_copy_present=False,
        )
        closed_missing = self.connection.execute(
            f"""
            UPDATE file_paths
            SET valid_to=%s
            WHERE vault_file_id=%s
              AND vault_id=%s
              AND path=%s
              AND valid_from=%s
              AND valid_to IS NULL
              AND ({consumed_pair})
            """,
            (
                changed_at,
                vault_file_id,
                vault_id,
                snapshot["missing_path"],
                snapshot["missing_path_valid_from"],
                *consumed_pair_params,
            ),
        )
        self._require_one_rename_mutation(closed_missing)

        closed_pair, closed_pair_params = self._rename_pair_predicate(
            snapshot,
            missing_path=missing_closed,
            provisional_status="retired",
            provisional_retired_at=changed_at,
            provisional_path=provisional_closed,
            provisional_copy_present=False,
        )
        inserted_path = self.connection.execute(
            f"""
            INSERT INTO file_paths(
                vault_file_id, vault_id, path, valid_from, valid_to
            )
            SELECT %s, %s, %s, %s, NULL
            WHERE ({closed_pair})
              AND NOT EXISTS (
                  SELECT 1
                  FROM file_paths current_path
                  WHERE current_path.vault_id=%s
                    AND current_path.path=%s
                    AND current_path.valid_to IS NULL
              )
            ON CONFLICT DO NOTHING
            """,
            (
                vault_file_id,
                vault_id,
                new_path,
                changed_at,
                *closed_pair_params,
                vault_id,
                new_path,
            ),
        )
        self._require_one_rename_mutation(inserted_path)

        renamed_pair, renamed_pair_params = self._rename_pair_predicate(
            snapshot,
            missing_path=missing_renamed,
            provisional_status="retired",
            provisional_retired_at=changed_at,
            provisional_path=provisional_closed,
            provisional_copy_present=False,
        )
        restored_copy = self.connection.execute(
            f"""
            UPDATE local_copies
            SET presence=%s,
                file_type=%s,
                size=%s,
                mtime_ns=%s,
                plaintext_sha256=%s,
                matched_archive_version_id=%s,
                last_seen_at=%s,
                observed_at=%s
            WHERE vault_file_id=%s
              AND ({renamed_pair})
            """,
            (
                snapshot["provisional_presence"],
                snapshot["provisional_file_type"],
                snapshot["provisional_size"],
                snapshot["provisional_mtime_ns"],
                snapshot["provisional_plaintext_sha256"],
                snapshot["provisional_matched_archive_version_id"],
                snapshot["provisional_last_seen_at"] or changed_at,
                snapshot["provisional_observed_at"] or changed_at,
                vault_file_id,
                *renamed_pair_params,
            ),
        )
        self._require_one_rename_mutation(restored_copy)
        # Mark after CAS succeeds so a failed confirmation never dirties, and
        # still inside the savepoint so dirty rows share rename rollback.
        invalidate_for_confirmed_rename(
            self.connection,
            vault_id,
            old_path=snapshot["missing_path"],
            provisional_path=snapshot["provisional_path"],
            new_path=new_path,
        )
        self.connection.execute("RELEASE SAVEPOINT rename_confirmation_cas")
        return vault_file_id

    def confirm_folder_rename(
        self,
        *,
        vault_id: int,
        old_prefix: str,
        new_prefix: str,
        changed_at: str,
    ) -> list[str]:
        """Rename every descendant under ``old_prefix`` in one logical batch."""
        old_prefix = old_prefix.strip("/")
        new_prefix = new_prefix.strip("/")
        if not old_prefix or not new_prefix:
            raise ValueError("Folder rename prefixes must be non-empty")
        if old_prefix == new_prefix:
            return []
        self._reserve_rename_confirmation(vault_id)
        escaped = (
            old_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        rows = self.connection.execute(
            """
            SELECT vf.id AS vault_file_id, fp.path
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id
             AND fp.vault_id=vf.vault_id
             AND fp.valid_to IS NULL
            WHERE vf.vault_id=%s
              AND vf.status='active'
              AND (fp.path=%s OR fp.path LIKE %s ESCAPE '\\')
            ORDER BY lower(fp.path)
            """,
            (vault_id, old_prefix, f"{escaped}/%"),
        ).fetchall()
        renamed_ids: list[str] = []
        for row in rows:
            path = row["path"]
            if path == old_prefix:
                new_path = new_prefix
            elif path.startswith(f"{old_prefix}/"):
                new_path = f"{new_prefix}/{path[len(old_prefix) + 1:]}"
            else:
                continue
            self.confirm_file_rename(
                vault_file_id=row["vault_file_id"],
                new_path=new_path,
                changed_at=changed_at,
                vault_id=vault_id,
            )
            renamed_ids.append(row["vault_file_id"])
        return renamed_ids

    def list_path_history(
        self, vault_file_id: str, *, vault_id: int
    ) -> list[dict[str, Any]]:
        return self.connection.execute(
            """
            SELECT fp.path, fp.valid_from, fp.valid_to
            FROM file_paths fp
            JOIN vault_files vf
              ON vf.id=fp.vault_file_id AND fp.vault_id=vf.vault_id
            WHERE fp.vault_file_id=%s AND vf.vault_id=%s
            ORDER BY fp.valid_from, fp.id
            """,
            (vault_file_id, vault_id),
        ).fetchall()

    def record_delete_marker(
        self,
        *,
        vault_id: int,
        path: str,
        object_key: str,
        provider_version_id: str,
        created_at: str,
        observed_at: str,
    ) -> str:
        file_id = self._resolve_cloud_path_file(vault_id, path, observed_at)
        existing = self.connection.execute(
            """
            SELECT id FROM delete_markers
            WHERE vault_id=%s AND object_key=%s AND provider_version_id=%s
            """,
            (vault_id, object_key, provider_version_id),
        ).fetchone()
        if existing:
            self.connection.execute(
                "UPDATE delete_markers SET discovered_at=%s WHERE id=%s",
                (observed_at, existing["id"]),
            )
            return existing["id"]
        marker_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO delete_markers(
                id, vault_file_id, vault_id, object_key, provider_version_id,
                created_at, discovered_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                marker_id,
                file_id,
                vault_id,
                object_key,
                provider_version_id,
                created_at,
                observed_at,
            ),
        )
        return marker_id

    def mark_unseen_archive_versions_missing(
        self, *, vault_id: int, scan_id: str, scan_started_at: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE archive_versions
            SET availability='missing', availability_checked_at=%s
            WHERE vault_file_id IN (
                SELECT id FROM vault_files WHERE vault_id=%s
            )
              AND (
                  availability_checked_at IS NULL
                  OR availability_checked_at<>%s
              )
              AND discovered_at<=%s
              AND availability<>'purged'
            """,
            (scan_id, vault_id, scan_id, scan_started_at),
        )
        # Bulk reconciliation can touch an unbounded set of paths.
        self._request_aggregate_rebuild(vault_id)

    def mark_unseen_local_copies_missing(
        self,
        *,
        vault_id: int,
        seen_at: str,
        observed_at: str,
        scan_started_at: str | None = None,
    ) -> None:
        """Mark only rows not changed after this scan generation began.

        A watcher or another completed scan may legitimately update a Local
        Copy while the walk is in progress.  Such a newer observation must not
        be overwritten by the older scan's final missing transition.
        """
        query = """
            UPDATE local_copies
            SET presence='missing', observed_at=%s
            WHERE vault_file_id IN (
                SELECT id FROM vault_files WHERE vault_id=%s
            )
              AND presence IN ('present', 'unsupported')
              AND (last_seen_at IS NULL OR last_seen_at<>%s)
        """
        params: list[Any] = [observed_at, vault_id, seen_at]
        if scan_started_at is not None:
            query += " AND (observed_at IS NULL OR observed_at<=%s)"
            params.append(scan_started_at)
        self.connection.execute(query, params)
        self._request_aggregate_rebuild(vault_id)

    def mark_local_path_missing(
        self, *, vault_id: int, path: str, observed_at: str
    ) -> None:
        escaped = (
            path.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        self.connection.execute(
            """
            UPDATE local_copies
            SET presence='missing', observed_at=%s
            WHERE vault_file_id IN (
                SELECT vf.id
                FROM vault_files vf
                JOIN file_paths fp
                  ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                WHERE vf.vault_id=%s
                  AND (fp.path=%s OR fp.path LIKE %s ESCAPE '\\')
            )
            """,
            (observed_at, vault_id, path, f"{escaped}/%"),
        )
        # Path may be a file or a directory prefix of many descendants.
        self._mark_path_aggregates_dirty(vault_id, path)
        mark_directory_dirty(self.connection, vault_id, path)

    def get_file_by_path(
        self, vault_id: int, path: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT
                vf.id,
                vf.vault_id,
                vf.status,
                fp.path,
                lc.presence AS local_presence,
                lc.file_type AS local_file_type,
                lc.size AS local_size,
                lc.mtime_ns AS local_mtime_ns,
                lc.plaintext_sha256 AS local_sha256,
                av.id AS version_id,
                av.version_number,
                av.object_key,
                av.provider_version_id,
                av.size AS version_size,
                av.storage_class,
                av.etag,
                av.plaintext_sha256 AS version_sha256,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
            LEFT JOIN archive_versions av
              ON av.vault_file_id=vf.id
             AND av.version_number=(
                 SELECT MAX(latest.version_number)
                 FROM archive_versions latest
                 WHERE latest.vault_file_id=vf.id
             )
            WHERE vf.vault_id=%s AND fp.path=%s
            """,
            (vault_id, path),
        ).fetchone()
        if row is None:
            return None
        local_copy = None
        if row["local_presence"] is not None:
            local_copy = {
                "presence": row["local_presence"],
                "file_type": row["local_file_type"],
                "size": row["local_size"],
                "mtime_ns": row["local_mtime_ns"],
                "plaintext_sha256": row["local_sha256"],
            }
        latest_version = None
        if row["version_id"] is not None:
            latest_version = {
                "id": row["version_id"],
                "version_number": row["version_number"],
                "object_key": row["object_key"],
                "provider_version_id": row["provider_version_id"],
                "size": row["version_size"],
                "storage_class": row["storage_class"],
                "etag": row["etag"],
                "plaintext_sha256": row["version_sha256"],
                "integrity": row["integrity"],
                "availability": row["availability"],
                "restore_state": row["restore_state"],
                "restore_expiry": row["restore_expiry"],
            }
        return {
            "id": row["id"],
            "vault_id": row["vault_id"],
            "status": row["status"],
            "path": row["path"],
            "local_copy": local_copy,
            "latest_version": latest_version,
        }

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
                av.etag,
                av.plaintext_sha256,
                av.uploaded_at,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry
            FROM archive_versions av
            JOIN vault_files vf ON vf.id=av.vault_file_id
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE vf.vault_id=%s AND fp.path=%s
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
        clauses = ["vf.vault_id=%s", "vf.status='active'"]
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
        rows = self.connection.execute(
            f"""
            SELECT
                fp.path,
                lc.presence AS local_presence,
                lc.file_type AS local_file_type,
                lc.size AS local_size,
                lc.plaintext_sha256 AS local_sha256,
                lc.matched_archive_version_id,
                av.id AS archive_version_id,
                av.size AS cloud_size,
                av.storage_class,
                av.plaintext_sha256 AS version_sha256,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry,
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
            LEFT JOIN archive_versions av
              ON av.id=(
                  SELECT latest.id
                  FROM archive_versions latest
                  WHERE latest.vault_file_id=vf.id
                    AND latest.availability NOT IN ('missing', 'purged')
                  ORDER BY latest.version_number DESC
                  LIMIT 1
              )
            WHERE {" AND ".join(clauses)}
            ORDER BY lower(fp.path)
            """,
            params,
        ).fetchall()
        result = []
        for row in rows:
            local_exists = row["local_presence"] in {"present", "unsupported"}
            cloud_exists = row["archive_version_id"] is not None
            if not local_exists and not cloud_exists:
                continue
            state = (
                "restoring"
                if row["restore_state"] == "restoring"
                else "both"
                if local_exists and cloud_exists
                else "local_only"
                if local_exists
                else "cloud_only"
            )
            upload_eligible = (
                row["local_presence"] == "present"
                and row["local_file_type"] == "regular"
                and not cloud_exists
            )
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
            lifecycle_pinned = is_path_pinned(
                self.connection, vault_id, row["path"]
            )
            storage_class_eligible = (
                cloud_exists
                and row["availability"] == "available"
                and row["restore_state"] != "restoring"
            )
            result.append(
                {
                    "path": row["path"],
                    "local_exists": int(local_exists),
                    "local_size": row["local_size"],
                    "local_file_type": row["local_file_type"],
                    "cloud_exists": int(cloud_exists),
                    "cloud_size": row["cloud_size"],
                    "storage_class": row["storage_class"],
                    "integrity": row["integrity"],
                    "availability": row["availability"],
                    "restore_state": row["restore_state"],
                    "restore_expiry": row["restore_expiry"],
                    "state": state,
                    "upload_eligible": upload_eligible,
                    "recover_eligible": recover_eligible,
                    "recoverable_version_count": recoverable_count,
                    "cleanup_eligible": cleanup_eligible,
                    "lifecycle_pinned": lifecycle_pinned,
                    "storage_class_eligible": storage_class_eligible,
                }
            )
        return result

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
        folder_total = count_child_directories(
            self.connection,
            vault_id,
            parent_path=parent_path,
            state_filter=state,
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
                state_filter=state,
                limit=page_size,
                offset=offset,
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
        clauses = ["vf.vault_id=%s", "vf.status='active'"]
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
            # Immediate children only (portable SQLite + PostgreSQL).
            clauses.append("fp.path LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}/%")
            clauses.append("fp.path NOT LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}/%/%")
        else:
            clauses.append("fp.path NOT LIKE %s")
            params.append("%/%")
        # Visibility and state classification mirror list_file_rows.
        # Presence may be NULL when no local_copies row exists — avoid SQL
        # three-valued NOT (NULL) filtering out legitimate cloud-only files.
        local_exists = (
            "coalesce(lc.presence, 'missing') IN ('present', 'unsupported')"
        )
        cloud_exists = "av.id IS NOT NULL"
        clauses.append(f"(({local_exists}) OR ({cloud_exists}))")
        if state:
            state_sql = {
                "restoring": "av.restore_state = 'restoring'",
                "both": (
                    f"({local_exists}) AND ({cloud_exists}) "
                    "AND coalesce(av.restore_state, '') <> 'restoring'"
                ),
                "local_only": (
                    f"({local_exists}) AND NOT ({cloud_exists}) "
                    "AND coalesce(av.restore_state, '') <> 'restoring'"
                ),
                "cloud_only": (
                    f"NOT ({local_exists}) AND ({cloud_exists}) "
                    "AND coalesce(av.restore_state, '') <> 'restoring'"
                ),
            }.get(state)
            if state_sql is None:
                clauses.append("1=0")
            else:
                clauses.append(f"({state_sql})")
        return clauses, params

    def _file_row_select_sql(self, clauses: list[str]) -> str:
        return f"""
            SELECT
                fp.path,
                lc.presence AS local_presence,
                lc.file_type AS local_file_type,
                lc.size AS local_size,
                lc.plaintext_sha256 AS local_sha256,
                lc.matched_archive_version_id,
                av.id AS archive_version_id,
                av.size AS cloud_size,
                av.storage_class,
                av.plaintext_sha256 AS version_sha256,
                av.integrity,
                av.availability,
                av.restore_state,
                av.restore_expiry,
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
            LEFT JOIN archive_versions av
              ON av.id=(
                  SELECT latest.id
                  FROM archive_versions latest
                  WHERE latest.vault_file_id=vf.id
                    AND latest.availability NOT IN ('missing', 'purged')
                  ORDER BY latest.version_number DESC
                  LIMIT 1
              )
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
            cloud_exists = row["archive_version_id"] is not None
            if not local_exists and not cloud_exists:
                continue
            state = (
                "restoring"
                if row["restore_state"] == "restoring"
                else "both"
                if local_exists and cloud_exists
                else "local_only"
                if local_exists
                else "cloud_only"
            )
            upload_eligible = (
                row["local_presence"] == "present"
                and row["local_file_type"] == "regular"
                and not cloud_exists
            )
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
            lifecycle_pinned = is_path_pinned(
                self.connection, vault_id, row["path"]
            )
            storage_class_eligible = (
                cloud_exists
                and row["availability"] == "available"
                and row["restore_state"] != "restoring"
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
                    "local_file_type": row["local_file_type"],
                    "cloud_exists": int(cloud_exists),
                    "cloud_size": row["cloud_size"],
                    "storage_class": row["storage_class"],
                    "integrity": row["integrity"],
                    "availability": row["availability"],
                    "restore_state": row["restore_state"],
                    "restore_expiry": row["restore_expiry"],
                    "state": state,
                    "upload_eligible": upload_eligible,
                    "recover_eligible": recover_eligible,
                    "recoverable_version_count": recoverable_count,
                    "cleanup_eligible": cleanup_eligible,
                    "lifecycle_pinned": lifecycle_pinned,
                    "storage_class_eligible": storage_class_eligible,
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
            LEFT JOIN archive_versions av
              ON av.id=(
                  SELECT latest.id
                  FROM archive_versions latest
                  WHERE latest.vault_file_id=vf.id
                    AND latest.availability NOT IN ('missing', 'purged')
                  ORDER BY latest.version_number DESC
                  LIMIT 1
              )
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
        sql = self._file_row_select_sql(clauses)
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
        # Search ignores the root-only instr() clause added for browse.
        # Rebuild clauses specifically for search.
        clauses = ["vf.vault_id=%s", "vf.status='active'"]
        params = [vault_id]
        clauses.append("lower(fp.path) LIKE lower(%s)")
        params.append(f"%{search}%")
        local_exists = (
            "coalesce(lc.presence, 'missing') IN ('present', 'unsupported')"
        )
        cloud_exists = "av.id IS NOT NULL"
        clauses.append(f"(({local_exists}) OR ({cloud_exists}))")
        if state:
            state_sql = {
                "restoring": "av.restore_state = 'restoring'",
                "both": (
                    f"({local_exists}) AND ({cloud_exists}) "
                    "AND coalesce(av.restore_state, '') <> 'restoring'"
                ),
                "local_only": (
                    f"({local_exists}) AND NOT ({cloud_exists}) "
                    "AND coalesce(av.restore_state, '') <> 'restoring'"
                ),
                "cloud_only": (
                    f"NOT ({local_exists}) AND ({cloud_exists}) "
                    "AND coalesce(av.restore_state, '') <> 'restoring'"
                ),
            }.get(state)
            if state_sql is None:
                clauses.append("1=0")
            else:
                clauses.append(f"({state_sql})")
        total_row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
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
                  ORDER BY latest.version_number DESC
                  LIMIT 1
              )
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchone()
        total = int(total_row["total"] or 0) if total_row else 0
        offset = (page - 1) * page_size
        sql = self._file_row_select_sql(clauses)
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
        predicate = f"""
            {runnable_sql}
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

    def list_claimable_jobs(
        self,
        *,
        now: str,
        restore_due_before: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Load scheduler candidates using the shared claimability definition."""
        if limit <= 0:
            return []
        predicate, params = self._claimable_job_predicate_sql(
            prefix="j.",
            now=now,
            restore_due_before=restore_due_before,
        )
        return self.connection.execute(
            f"""
            SELECT j.*, v.source_root, v.s3_bucket,
                   v.s3_prefix, v.rclone_remote, v.encryption_mode,
                   v.crypt_password_ciphertext, v.crypt_password2_ciphertext,
                   v.uuid AS vault_uuid, v.name AS vault_name,
                   v.cloud_deletion_enabled, v.decommission_state
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE v.relocation_state='ready'
              AND (
                    (v.enabled=TRUE AND v.decommission_state='active'
                     AND j.origin<>'decommission')
                 OR (v.decommission_state='decommissioning'
                     AND j.origin='decommission')
              )
              AND ({predicate})
            ORDER BY j.requested_at ASC, j.id ASC
            LIMIT {int(limit)}
            """,
            params,
        ).fetchall()

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

    def _job_claim_backend(self) -> str:
        return str(getattr(self.connection, "backend", "postgresql") or "postgresql")

    def claim_job(
        self,
        *,
        job_id: int,
        claim_token: str,
        claimed_at: str,
        claim_expires_at: str,
        now: str,
        restore_due_before: str,
    ) -> dict[str, Any] | None:
        """Atomically lease one runnable Job.

        The conditional UPDATE is the acquisition primitive for both SQLite and
        PostgreSQL.  SQLite serializes writers; PostgreSQL locks the matching
        row while it rechecks the predicate.  A loser therefore observes no
        returned row instead of executing a stale in-memory selection.
        """
        if not claim_token:
            raise ValueError("A Job claim token is required")
        claimable_sql, claimable_params = self._claimable_job_predicate_sql(
            prefix="",
            now=now,
            restore_due_before=restore_due_before,
        )
        return self.connection.execute(
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
    ) -> list[dict[str, Any]]:
        """Lease every active Job in one cloud-purge group as one operation.

        A permanent purge may span many Vault Files.  Its group is only
        executable when every non-terminal member is due and unclaimed (or its
        prior lease has expired).  SQLite obtains the writer reservation before
        inspecting the group; PostgreSQL locks the group rows in deterministic
        ``requested_at, id`` order.  This prevents two workers from splitting a
        destructive group between themselves.
        """
        if not claim_token:
            raise ValueError("A Job claim token is required")
        backend = self._job_claim_backend()
        raw_connection = getattr(self.connection, "connection", None)
        if (
            backend == "sqlite"
            and raw_connection is not None
            and not getattr(raw_connection, "in_transaction", False)
        ):
            self.connection.begin_immediate()

        lock = " FOR UPDATE" if backend != "sqlite" else ""
        # Do not lock the nominated lead first on PostgreSQL: two schedulers can
        # nominate different members of the same group.  Both instead acquire
        # the full group below in requested_at/id order, avoiding an AB/BA row
        # lock deadlock while still rechecking the durable group identity.
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
        if backend != "sqlite":
            # Do not block a scheduler thread behind another worker's whole
            # destructive group.  The transaction-scoped advisory lock is a
            # stable provider-independent key; a loser simply tries another
            # fair candidate on the next poll, while row locks below still
            # protect cancellation and non-cooperating writers.
            advisory = self.connection.execute(
                """
                SELECT pg_try_advisory_xact_lock(
                    hashtextextended(%s, 0)
                ) AS acquired
                """,
                (f"frostvault:cloud-purge:{lead['vault_id']}:{lead['group_id']}",),
            ).fetchone()
            if not advisory or not advisory["acquired"]:
                return []
        rows = self.connection.execute(
            f"""
            SELECT id, status, pending_until, claim_token, claim_expires_at,
                   requested_at
            FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
            ORDER BY requested_at ASC, id ASC{lock}
            """,
            (lead["vault_id"], lead["group_id"]),
        ).fetchall()
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
    ) -> int:
        """Count runnable, currently unleased queued Job rows.

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
                     AND j.origin<>'decommission')
                 OR (v.decommission_state='decommissioning'
                     AND j.origin='decommission')
              )
              AND ({predicate})
            """,
            params,
        ).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    def summary(self, vault_id: int) -> dict[str, Any]:
        """Return archive state counts and byte totals via SQL aggregates.

        Intentionally avoids ``list_file_rows`` and any Python-side walk of
        Vault File rows so ``/api/stats`` stays bounded on large catalogs.
        State classification mirrors ``list_file_rows`` (restoring / both /
        local_only / cloud_only) and skips rows with neither a Local Copy nor a
        non-missing/purged Archive Version.
        """
        rows = self.connection.execute(
            """
            WITH latest_versions AS (
                SELECT
                    av.vault_file_id AS vault_file_id,
                    av.id AS archive_version_id,
                    av.size AS cloud_size,
                    av.restore_state AS restore_state,
                    ROW_NUMBER() OVER (
                        PARTITION BY av.vault_file_id
                        ORDER BY av.version_number DESC
                    ) AS rn
                FROM archive_versions av
                WHERE av.vault_id=%s
                  AND av.availability NOT IN ('missing', 'purged')
            )
            SELECT
                classified.state AS state,
                COUNT(*) AS total,
                COALESCE(SUM(classified.local_bytes), 0) AS local_bytes,
                COALESCE(SUM(classified.cloud_bytes), 0) AS cloud_bytes
            FROM (
                SELECT
                    CASE
                        WHEN latest.restore_state = 'restoring' THEN 'restoring'
                        WHEN (
                            lc.presence IN ('present', 'unsupported')
                            AND latest.archive_version_id IS NOT NULL
                        ) THEN 'both'
                        WHEN lc.presence IN ('present', 'unsupported') THEN 'local_only'
                        WHEN latest.archive_version_id IS NOT NULL THEN 'cloud_only'
                    END AS state,
                    CASE
                        WHEN lc.presence IN ('present', 'unsupported')
                        THEN COALESCE(lc.size, 0)
                        ELSE 0
                    END AS local_bytes,
                    CASE
                        WHEN latest.archive_version_id IS NOT NULL
                        THEN COALESCE(latest.cloud_size, 0)
                        ELSE 0
                    END AS cloud_bytes
                FROM vault_files vf
                JOIN file_paths fp
                  ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
                LEFT JOIN latest_versions latest
                  ON latest.vault_file_id=vf.id AND latest.rn=1
                WHERE vf.vault_id=%s
                  AND vf.status='active'
                  AND (
                        lc.presence IN ('present', 'unsupported')
                     OR latest.archive_version_id IS NOT NULL
                  )
            ) AS classified
            GROUP BY classified.state
            """,
            (vault_id, vault_id),
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
        return {
            "states": states,
            "storage": {
                "local_bytes": local_bytes,
                "cloud_bytes": cloud_bytes,
            },
            "active_jobs": int(active_jobs),
        }

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
    ) -> tuple[list[int], int, int]:
        # This write is deliberately first: PostgreSQL takes a row lock and
        # SQLite takes its write lock before the usage snapshot and inserts.
        # Every manual upload/recovery/free-space admission therefore observes
        # one serialized Vault state.
        lock_vault(
            self.connection,
            vault_id,
            allow_decommission=origin == "decommission",
        )
        self.last_skipped_same_class = 0
        vault = self.connection.execute(
            """
            SELECT decommission_state, encryption_mode,
                   recovery_custody_confirmed_at
            FROM vaults WHERE id=%s
            """,
            (vault_id,),
        ).fetchone()
        if vault is None:
            raise ValueError(f"Vault {vault_id} not found")
        lifecycle = str(vault.get("decommission_state") or "active")
        if lifecycle != "active" and not (
            lifecycle == "decommissioning" and origin == "decommission"
        ):
            raise ValueError("Vault is quiesced for decommission")
        if origin == "decommission" and lifecycle != "decommissioning":
            raise ValueError("Decommission Jobs require a quiesced Vault")
        if action == "upload":
            require_upload_custody(vault)
        clauses = ["vf.vault_id=%s", "vf.status='active'"]
        params: list[Any] = [vault_id]
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
        if whole_vault:
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
        version_select_actions = {"recover", "storage-class"}
        if action in version_select_actions and archive_version_id and not is_directory and not whole_vault:
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
        skipped_same = 0
        target_class = (target_storage_class or "").upper() or None
        for row in candidates:
            if action == "upload":
                allowed = (
                    row["presence"] == "present"
                    and row["file_type"] == "regular"
                    and row["archive_version_id"] is None
                )
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

        storage_unknown = action == "upload" and any(
            row["local_size"] is None for row in eligible
        )
        storage_growth = sum(
            int(row["local_size"] or 0) for row in eligible
        ) if action == "upload" else 0
        restore_unknown = action == "recover" and any(
            row["cloud_size"] is None for row in eligible
        )
        restore_growth = sum(
            int(row["cloud_size"] or 0) for row in eligible
        ) if action == "recover" else 0
        try:
            self.last_quota_evaluation = admit_quota(
                self.connection,
                vault_id,
                action=action,
                candidate_count=len(eligible),
                storage_growth_bytes=storage_growth,
                storage_size_unknown=storage_unknown,
                restore_growth_bytes=restore_growth,
                restore_request_unknown=restore_unknown,
                lock=False,
            )
        except QuotaBlocked as blocked:
            audit_log(
                "quota_hard_block",
                connection=self.connection,
                vault_id=vault_id,
                actor_user_id=requested_by,
                outcome="blocked",
                visibility="vault",
                action=action,
                decisions=[item.as_dict() for item in blocked.evaluation.decisions],
            )
            raise
        if self.last_quota_evaluation.warnings:
            audit_log(
                "quota_soft_warning",
                connection=self.connection,
                vault_id=vault_id,
                actor_user_id=requested_by,
                outcome="warned",
                visibility="vault",
                action=action,
                decisions=[
                    item.as_dict() for item in self.last_quota_evaluation.warnings
                ],
            )

        job_ids: list[int] = []
        total_bytes = 0
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
                    estimated_hours, pending_until, origin, target_storage_class
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, 0,
                    %s, %s, %s, %s, %s, %s, %s
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
                    restore_tier if action in {"recover", "storage-class"} else None,
                    restore_days if action in {"recover", "storage-class"} else None,
                    estimated_cost_eur if action in {"recover", "storage-class"} else None,
                    estimated_hours if action in {"recover", "storage-class"} else None,
                    pending_until if action == "recover" else None,
                    origin,
                    target_class if action == "storage-class" else None,
                ),
            ).fetchone()
            if job is None:
                continue
            job_ids.append(int(job["id"]))
            total_bytes += size
        return job_ids, total_bytes, len(eligible)
