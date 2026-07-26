from __future__ import annotations

import uuid
from typing import Any

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


class ArchiveCatalog:
    """Keep versioned file invariants behind one persistence interface."""

    def __init__(self, connection: Any):
        self.connection = connection
        self.last_quota_evaluation = QuotaEvaluation(allowed=True)
        self.last_skipped_same_class = 0

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
        return version_id

    def link_job_version(self, job_id: int, archive_version_id: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET archive_version_id=%s WHERE id=%s",
            (archive_version_id, job_id),
        )

    def get_job_target(self, job_id: int) -> dict[str, Any] | None:
        return self.connection.execute(
            """
            SELECT
                j.id AS job_id,
                j.vault_id,
                j.vault_file_id,
                j.archive_version_id,
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

    def mark_local_copy_missing(
        self, vault_file_id: str, *, observed_at: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE local_copies
            SET presence='missing', observed_at=%s
            WHERE vault_file_id=%s
            """,
            (observed_at, vault_file_id),
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
        self.connection.execute(
            """
            UPDATE archive_versions
            SET plaintext_sha256=%s, integrity='verified', verified_at=%s
            WHERE id=%s
            """,
            (plaintext_sha256.lower(), verified_at, archive_version_id),
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
        self.connection.execute(
            """
            UPDATE archive_versions
            SET plaintext_sha256=%s, integrity='mismatch', verified_at=%s
            WHERE id=%s
            """,
            (plaintext_sha256.lower(), checked_at, archive_version_id),
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

    def rename_file(
        self,
        vault_file_id: str,
        *,
        new_path: str,
        changed_at: str,
    ) -> None:
        current = self.connection.execute(
            """
            SELECT fp.path
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE vf.id=%s AND vf.status='active'
            """,
            (vault_file_id,),
        ).fetchone()
        if current is None:
            raise LookupError(f"Active Vault File not found: {vault_file_id}")
        if current["path"] == new_path:
            return
        self.connection.execute(
            """
            UPDATE file_paths SET valid_to=%s
            WHERE vault_file_id=%s AND valid_to IS NULL
            """,
            (changed_at, vault_file_id),
        )
        vault = self.connection.execute(
            "SELECT vault_id FROM vault_files WHERE id=%s",
            (vault_file_id,),
        ).fetchone()
        self.connection.execute(
            """
            INSERT INTO file_paths(
                vault_file_id, vault_id, path, valid_from, valid_to
            ) VALUES (%s, %s, %s, %s, NULL)
            """,
            (vault_file_id, vault["vault_id"], new_path, changed_at),
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
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
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

    def confirm_file_rename(
        self,
        *,
        vault_file_id: str,
        new_path: str,
        changed_at: str,
    ) -> str:
        """Keep one Vault File identity and absorb a provisional new-path file."""
        current = self.connection.execute(
            """
            SELECT vf.vault_id, fp.path
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE vf.id=%s AND vf.status='active'
            """,
            (vault_file_id,),
        ).fetchone()
        if current is None:
            raise LookupError(f"Active Vault File not found: {vault_file_id}")
        if current["path"] == new_path:
            return vault_file_id

        provisional = self.connection.execute(
            """
            SELECT vf.id AS vault_file_id,
                   lc.presence, lc.file_type, lc.size, lc.mtime_ns,
                   lc.plaintext_sha256, lc.matched_archive_version_id,
                   lc.last_seen_at, lc.observed_at
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
            WHERE vf.vault_id=%s AND vf.status='active' AND fp.path=%s
            """,
            (current["vault_id"], new_path),
        ).fetchone()

        local_update = None
        if provisional is not None and provisional["vault_file_id"] != vault_file_id:
            local_update = provisional
            self.connection.execute(
                """
                UPDATE file_paths SET valid_to=%s
                WHERE vault_file_id=%s AND valid_to IS NULL
                """,
                (changed_at, provisional["vault_file_id"]),
            )
            self.connection.execute(
                "DELETE FROM local_copies WHERE vault_file_id=%s",
                (provisional["vault_file_id"],),
            )
            self.connection.execute(
                """
                UPDATE vault_files
                SET status='retired', retired_at=%s
                WHERE id=%s
                """,
                (changed_at, provisional["vault_file_id"]),
            )

        self.rename_file(
            vault_file_id, new_path=new_path, changed_at=changed_at
        )

        if local_update is not None and local_update["presence"] is not None:
            self.connection.execute(
                """
                INSERT INTO local_copies(
                    vault_file_id, presence, file_type, size, mtime_ns,
                    plaintext_sha256, matched_archive_version_id,
                    last_seen_at, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(vault_file_id) DO UPDATE SET
                    presence=excluded.presence,
                    file_type=excluded.file_type,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    plaintext_sha256=excluded.plaintext_sha256,
                    matched_archive_version_id=excluded.matched_archive_version_id,
                    last_seen_at=excluded.last_seen_at,
                    observed_at=excluded.observed_at
                """,
                (
                    vault_file_id,
                    local_update["presence"],
                    local_update["file_type"],
                    local_update["size"],
                    local_update["mtime_ns"],
                    local_update["plaintext_sha256"],
                    local_update["matched_archive_version_id"],
                    local_update["last_seen_at"] or changed_at,
                    local_update["observed_at"] or changed_at,
                ),
            )
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
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
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
            )
            renamed_ids.append(row["vault_file_id"])
        return renamed_ids

    def list_path_history(self, vault_file_id: str) -> list[dict[str, Any]]:
        return self.connection.execute(
            """
            SELECT path, valid_from, valid_to
            FROM file_paths
            WHERE vault_file_id=%s
            ORDER BY valid_from, id
            """,
            (vault_file_id,),
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

    def mark_unseen_local_copies_missing(
        self, *, vault_id: int, seen_at: str, observed_at: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE local_copies
            SET presence='missing', observed_at=%s
            WHERE vault_file_id IN (
                SELECT id FROM vault_files WHERE vault_id=%s
            )
              AND presence IN ('present', 'unsupported')
              AND (last_seen_at IS NULL OR last_seen_at<>%s)
            """,
            (observed_at, vault_id, seen_at),
        )

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

    def summary(self, vault_id: int) -> dict[str, Any]:
        rows = self.list_file_rows(vault_id)
        states: dict[str, int] = {}
        local_bytes = 0
        cloud_bytes = 0
        for row in rows:
            states[row["state"]] = states.get(row["state"], 0) + 1
            if row["local_exists"]:
                local_bytes += int(row["local_size"] or 0)
            if row["cloud_exists"]:
                cloud_bytes += int(row["cloud_size"] or 0)
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
        lock_vault(self.connection, vault_id)
        self.last_skipped_same_class = 0
        if action == "upload":
            vault = self.connection.execute(
                """
                SELECT encryption_mode, recovery_custody_confirmed_at
                FROM vaults WHERE id=%s
                """,
                (vault_id,),
            ).fetchone()
            if vault is not None:
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
                    restore_tier if action == "recover" else None,
                    restore_days if action == "recover" else None,
                    estimated_cost_eur if action == "recover" else None,
                    estimated_hours if action == "recover" else None,
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
