"""0057 migration safety on disposable PostgreSQL (PR CI only)."""
from pathlib import Path
import tempfile
import unittest

from app.catalog import ArchiveCatalog
from tests.postgres import PostgresConnection, run_alembic

STAMP = "2026-11-03T00:00:00+00:00"


class LocalReleaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "release.db"
        result = run_alembic(self.path, "0056_notification_problems")
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(self.path) as connection:
            connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES(1,'owner','Owner','hash')")
            for i, (enabled, period) in enumerate(((False, None), (False, 180), (True, 90), (True, 180),
                                                  (True, 365), (True, 730), (True, 17), (True, None)), 1):
                connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES(%s,%s,%s,'/source','bucket',%s)",
                                   (i, f"v{i}", f"Vault {i}", f"vaults/{i}"))
                connection.execute("INSERT INTO vault_operation_policies(vault_id,auto_upload,auto_local_cleanup,local_retention_days,include_globs_json,exclude_globs_json) "
                                   "VALUES(%s,FALSE,%s,%s,'[\"*.jpg\"]','[\"keep/**\"]')", (i, enabled, period))
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(vault_id=1, path="photo.jpg", file_type="regular", size=5, mtime_ns=1, observed_at=STAMP)
            version = catalog.record_archive_version(vault_id=1, path="photo.jpg", object_key="vaults/1/photo.jpg", provider_version_id="only-copy",
                                                     size=5, storage_class="DEEP_ARCHIVE", etag="etag", uploaded_at=STAMP, observed_at=STAMP, scan_id=STAMP)
            catalog.mark_version_verified(version, plaintext_sha256="a" * 64, verified_at=STAMP)
            catalog.set_local_fingerprint(vault_id=1, path="photo.jpg", plaintext_sha256="a" * 64, matched_archive_version_id=version)
            connection.execute("UPDATE local_copies SET recovered_at=%s WHERE vault_file_id=%s", (STAMP, file_id))
            for origin in ("manual", "automatic", "decommission"):
                job_file_id = catalog.observe_local_copy(vault_id=1, path=origin, file_type="regular", size=5, mtime_ns=1, observed_at=STAMP)
                connection.execute("INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at,origin) "
                                   "VALUES(1,%s,%s,'free-space','queued',1,%s,%s,%s)", (job_file_id, origin, STAMP, STAMP, origin))
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at) "
                               "VALUES(1,%s,'finished','free-space','completed',1,%s,%s)", (file_id, STAMP, STAMP))

    def evidence(self, connection):
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY id" if table != "local_copies" else f"SELECT * FROM {table} ORDER BY vault_file_id").fetchall()
                for table in ("vaults", "vault_files", "file_paths", "archive_versions", "local_copies")}

    def test_all_legacy_combinations_are_off_and_only_pending_release_jobs_cancelled(self):
        with PostgresConnection(self.path) as connection:
            before = self.evidence(connection)
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(self.evidence(connection), before)
            policies = connection.execute("SELECT * FROM vault_operation_policies ORDER BY vault_id").fetchall()
            self.assertEqual(len(policies), 8)
            for policy in policies:
                self.assertIsNone(policy["local_release_after_days"])
                self.assertNotIn("auto_local_cleanup", policy)
                self.assertNotIn("local_retention_days", policy)
                self.assertFalse(policy["auto_upload"])
                self.assertEqual(policy["include_globs_json"], '["*.jpg"]')
                self.assertEqual(policy["exclude_globs_json"], '["keep/**"]')
            statuses = connection.execute("SELECT path,status FROM jobs ORDER BY id").fetchall()
            self.assertEqual([row["status"] for row in statuses], ["cancelled"] * 3 + ["completed"])
        result = run_alembic(self.path, "0056_notification_problems", "downgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(self.evidence(connection), before)
            self.assertEqual(connection.execute("SELECT count(*) AS total FROM vault_operation_policies WHERE auto_local_cleanup").fetchone()["total"], 0)

    def test_interrupted_rename_claim_refuses_upgrade_without_mutating_anything(self):
        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE jobs SET status='cleaning' WHERE path='manual'")
            before = self.evidence(connection)
            jobs_before = connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        result = run_alembic(self.path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Reconcile interrupted free-space Jobs", result.stderr)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(self.evidence(connection), before)
            self.assertEqual(connection.execute("SELECT * FROM jobs ORDER BY id").fetchall(), jobs_before)
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0056_notification_problems")
