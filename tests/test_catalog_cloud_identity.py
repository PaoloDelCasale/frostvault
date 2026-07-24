"""Cloud rediscovery must preserve Vault File identity (BUG-008 / REQ-020).

Seams under test:
- ArchiveCatalog.record_archive_version — public catalog API used by cloud scan
  (scan_cloud). After a confirmed rename, rediscovering an old S3 key at a
  historical path must reuse the existing Vault File / Archive Version rather
  than minting an orphan identity. Observed through get_file_by_path and the
  returned Archive Version id (no static source inspection).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from tests.test_database import run_alembic


class CloudRediscoveryIdentityTests(unittest.TestCase):
    """BUG-008: cloud scan must not fork Vault File identity after rename."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog.db"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

    def test_bug_008_cloud_record_reuses_identity_before_create(self) -> None:
        """[BUG-008][Req: REQ-020] Path History identity survives cloud rediscovery.

        Seam: ``record_archive_version`` after ``confirm_file_rename``.
        Post-rename rediscovery of the old object_key/VersionId at the
        historical path must not mint a new current Vault File at the old path;
        the Archive Version stays on the renamed identity.
        """
        old_path = "reports/q1.pdf"
        new_path = "archive/q1.pdf"
        object_key = "docs/reports/q1.pdf"
        provider_version_id = "s3-v-old"

        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=2,
                path=old_path,
                file_type="regular",
                size=32,
                mtime_ns=1_000,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            version_id = catalog.record_archive_version(
                vault_id=2,
                path=old_path,
                object_key=object_key,
                provider_version_id=provider_version_id,
                size=32,
                storage_class="STANDARD",
                etag="etag-old",
                uploaded_at="2026-07-21T10:01:00+00:00",
                observed_at="2026-07-21T10:01:00+00:00",
                scan_id="2026-07-21T10:01:00+00:00",
                origin="upload",
            )
            catalog.confirm_file_rename(
                vault_file_id=file_id,
                new_path=new_path,
                changed_at="2026-07-21T11:00:00+00:00",
            )
            self.assertEqual(
                catalog.get_file_by_path(2, new_path)["id"],
                file_id,
            )
            self.assertIsNone(catalog.get_file_by_path(2, old_path))

            # Cloud scan rediscovers the still-present old S3 key.
            returned = catalog.record_archive_version(
                vault_id=2,
                path=old_path,
                object_key=object_key,
                provider_version_id=provider_version_id,
                size=32,
                storage_class="STANDARD",
                etag="etag-old",
                uploaded_at="2026-07-21T10:01:00+00:00",
                observed_at="2026-07-21T12:00:00+00:00",
                scan_id="2026-07-21T12:00:00+00:00",
                origin="discovered",
            )

            self.assertEqual(returned, version_id)
            # Historical path must not become a new current Vault File.
            self.assertIsNone(
                catalog.get_file_by_path(2, old_path),
                "cloud rediscovery must not mint an orphan Vault File at the old path",
            )
            current = catalog.get_file_by_path(2, new_path)
            self.assertIsNotNone(current)
            self.assertEqual(current["id"], file_id)
            active = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM vault_files
                WHERE vault_id=%s AND status='active'
                """,
                (2,),
            ).fetchone()
            self.assertEqual(int(active["count"]), 1)

    def test_bug_008_new_cloud_version_at_historical_path_reuses_identity(
        self,
    ) -> None:
        """[BUG-008][Req: REQ-020] New cloud VersionId at a historical path
        attaches to the active Vault File that still carries that path in
        Path History, instead of minting an orphan current identity.
        """
        old_path = "reports/q2.pdf"
        new_path = "archive/q2.pdf"

        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=2,
                path=old_path,
                file_type="regular",
                size=16,
                mtime_ns=2_000,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.record_archive_version(
                vault_id=2,
                path=old_path,
                object_key="docs/reports/q2.pdf",
                provider_version_id="s3-v-1",
                size=16,
                storage_class="STANDARD",
                etag="etag-1",
                uploaded_at="2026-07-21T10:01:00+00:00",
                observed_at="2026-07-21T10:01:00+00:00",
                scan_id="2026-07-21T10:01:00+00:00",
                origin="upload",
            )
            catalog.confirm_file_rename(
                vault_file_id=file_id,
                new_path=new_path,
                changed_at="2026-07-21T11:00:00+00:00",
            )

            new_version_id = catalog.record_archive_version(
                vault_id=2,
                path=old_path,
                object_key="docs/reports/q2.pdf",
                provider_version_id="s3-v-2",
                size=16,
                storage_class="STANDARD",
                etag="etag-2",
                uploaded_at="2026-07-21T12:00:00+00:00",
                observed_at="2026-07-21T12:00:00+00:00",
                scan_id="2026-07-21T12:00:00+00:00",
                origin="discovered",
            )

            self.assertIsNone(catalog.get_file_by_path(2, old_path))
            self.assertEqual(catalog.get_file_by_path(2, new_path)["id"], file_id)
            row = connection.execute(
                """
                SELECT vault_file_id FROM archive_versions WHERE id=%s
                """,
                (new_version_id,),
            ).fetchone()
            self.assertEqual(row["vault_file_id"], file_id)
            active = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM vault_files
                WHERE vault_id=%s AND status='active'
                """,
                (2,),
            ).fetchone()
            self.assertEqual(int(active["count"]), 1)
