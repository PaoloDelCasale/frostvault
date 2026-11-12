"""Routine automatic renames are not logged (#416).

The #188 confirmation races moved to ``tests/test_catalog_rename`` (#587).
"""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import hashlib
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import settings

from app.security import hash_password
from app.storage import apply_auto_renames
from tests.test_database import run_alembic

DIGEST_A = hashlib.sha256(b"content-a").hexdigest()


class RenameAuditPersistenceTests(unittest.TestCase):
    """Internal automatic rename transactions remain race-safe and unlogged."""

    PASSWORD = "correct-horse-battery"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        self.source_root = Path(self._tmp.name) / "source"
        self.source_root.mkdir()
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with PostgresConnection(str(self.database_path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash)
                VALUES (%s, %s, %s) RETURNING id
                """,
                ("owner", "Owner", hash_password(self.PASSWORD)),
            ).fetchone()["id"]
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix) VALUES (
                    'docs', 'Docs', %s, 'bucket', 'docs') RETURNING id
                """,
                (str(self.source_root),),
            ).fetchone()["id"]

        self.test_settings = replace(
            settings,
            database_dsn=database_dsn(str(self.database_path)),
            cookie_secure=False,
        )
        for target in ("app.database.settings", "app.storage.settings"):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)






    def _seed_digest_rename(
        self,
        *,
        old_path: str,
        new_path: str,
        digest: str = DIGEST_A,
        vault_id: int | None = None,
        with_verified_version: bool = False,
    ) -> str:
        target_vault_id = self.vault_id if vault_id is None else vault_id
        with PostgresConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=target_vault_id,
                path=old_path,
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            archive_version_id = None
            if with_verified_version:
                archive_version_id = catalog.record_archive_version(
                    vault_id=target_vault_id,
                    path=old_path,
                    object_key=f"docs/{old_path}",
                    provider_version_id=f"rename-{uuid.uuid4()}",
                    size=9,
                    storage_class="STANDARD",
                    etag="rename-etag",
                    uploaded_at="2026-07-21T10:01:00+00:00",
                    observed_at="2026-07-21T10:01:00+00:00",
                    scan_id="2026-07-21T10:01:00+00:00",
                    origin="upload",
                )
                catalog.mark_version_verified(
                    archive_version_id,
                    plaintext_sha256=digest,
                    verified_at="2026-07-21T10:02:00+00:00",
                )
            catalog.set_local_fingerprint(
                vault_id=target_vault_id,
                path=old_path,
                plaintext_sha256=digest,
                matched_archive_version_id=archive_version_id,
            )
            catalog.mark_local_copy_missing(
                old_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=target_vault_id,
                path=new_path,
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=target_vault_id,
                path=new_path,
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            return old_id




    def test_routine_auto_rename_does_not_create_action_log_entries(self) -> None:
        old_path = "auto/old.txt"
        new_path = "auto/new.txt"
        payload = b"content-a"
        new_file = self.source_root / new_path
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_bytes(payload)

        with PostgresConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=self.vault_id,
                path=old_path,
                file_type="regular",
                size=len(payload),
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=self.vault_id,
                path=old_path,
                plaintext_sha256=DIGEST_A,
                matched_archive_version_id=None,
            )
            catalog.mark_local_copy_missing(
                old_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=self.vault_id,
                path=new_path,
                file_type="regular",
                size=len(payload),
                mtime_ns=new_file.stat().st_mtime_ns,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            # Leave new-path fingerprint unset so apply_auto_renames hashes it.

        vault = {
            "id": self.vault_id,
            "source_root": str(self.source_root),
        }
        # Keep the scan timestamp correlated with the disappearance fixture;
        # automatic rename matching intentionally rejects stale digest matches.
        with patch(
            "app.storage.now_iso",
            return_value="2026-07-21T11:00:00+00:00",
        ):
            summary = apply_auto_renames(vault, requested_by=self.owner_id)
        self.assertGreaterEqual(summary["confirmed"], 1)

        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS n FROM audit_events WHERE event='vault_file_renamed'"
            ).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
