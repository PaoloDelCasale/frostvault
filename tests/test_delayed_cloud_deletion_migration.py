"""0059 populated upgrade/downgrade/re-upgrade and refusal gates (PR PostgreSQL)."""
import tempfile
import unittest
from pathlib import Path

from tests.historical_catalog_fixture import HistoricalCatalogFixture as ArchiveCatalog
from tests.postgres import PostgresConnection
from tests.test_database import run_alembic


class DelayedDeletionMigrationTests(unittest.TestCase):
    def test_legacy_delays_cancel_without_touching_version_evidence_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            result = run_alembic(path, "0058_reconnect_recovery_kit")
            self.assertEqual(result.returncode,0,result.stderr)
            with PostgresConnection(str(path)) as connection:
                connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (1,'test','Test','/sources/test','bucket','vaults/test/')")
                connection.execute("INSERT INTO vault_files(id,vault_id,status,created_at) VALUES ('file',1,'active','2026-11-05T00:00:00+00:00')")
                connection.execute("""INSERT INTO jobs(id,vault_id,vault_file_id,path,action,status,requested_at,updated_at,group_id,pending_until)
                    VALUES (1,1,'file','photo.jpg','cloud-purge','pending_delay','2026-11-05T00:00:00+00:00','2026-11-05T00:00:00+00:00','legacy','2026-11-06T00:00:00+00:00')""")
                connection.execute("""INSERT INTO cloud_deletion_items(job_id,vault_id,vault_file_id,kind,object_key,provider_version_id,size_bytes,status,updated_at)
                    VALUES (1,1,'file','version','vaults/test/photo.jpg','exact-version',5,'pending','2026-11-05T00:00:00+00:00')""")
                catalog = ArchiveCatalog(connection)
                version = catalog.record_archive_version(vault_id=1, path="evidence.jpg",
                    object_key="vaults/test/evidence.jpg", provider_version_id="sole-cloud-copy", size=5,
                    storage_class="STANDARD", etag="etag", uploaded_at="2026-11-05T00:00:00+00:00",
                    observed_at="2026-11-05T00:00:00+00:00", scan_id="scan", origin="upload")
                catalog.mark_version_verified(version, plaintext_sha256="a"*64, verified_at="2026-11-05T00:00:00+00:00")
            # This contract owns 0059, not populated adoption into today's HEAD.
            result = run_alembic(path, "0059_delayed_cloud_deletion")
            self.assertEqual(result.returncode,0,result.stderr)
            with PostgresConnection(str(path)) as connection:
                self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"],"cancelled")
                item = connection.execute("SELECT * FROM cloud_deletion_items").fetchone()
                self.assertEqual((item["status"],item["provider_version_id"]),("skipped","exact-version"))
                evidence = connection.execute("SELECT integrity,availability,provider_version_id FROM archive_versions WHERE id=%s", (version,)).fetchone()
                self.assertEqual((evidence["integrity"],evidence["availability"],evidence["provider_version_id"]),
                                 ("verified","available","sole-cloud-copy"))
                self.assertIsNone(connection.execute("SELECT root_released_at FROM vaults").fetchone()["root_released_at"])
                self.assertFalse(connection.execute("""SELECT EXISTS(SELECT 1 FROM information_schema.columns
                    WHERE table_schema=current_schema() AND table_name='vaults' AND column_name='cloud_deletion_enabled') AS present""").fetchone()["present"])
            result = run_alembic(path,"0058_reconnect_recovery_kit",command="downgrade")
            self.assertEqual(result.returncode,0,result.stderr)
            result = run_alembic(path, "0059_delayed_cloud_deletion")
            self.assertEqual(result.returncode,0,result.stderr)
            with PostgresConnection(str(path)) as connection:
                self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"],"cancelled")

    def test_interrupted_destructive_work_refuses_upgrade_before_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            self.assertEqual(run_alembic(path,"0058_reconnect_recovery_kit").returncode,0)
            with PostgresConnection(str(path)) as connection:
                connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (1,'test','Test','/sources/test','bucket','vaults/test/')")
                connection.execute("INSERT INTO vault_files(id,vault_id,status,created_at) VALUES ('file',1,'active','2026-11-05T00:00:00+00:00')")
                connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at)
                    VALUES (1,'file','photo.jpg','cloud-purge','cleaning','2026-11-05T00:00:00+00:00','2026-11-05T00:00:00+00:00')""")
            result = run_alembic(path)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("Reconcile interrupted cloud deletions",result.stderr)
            with PostgresConnection(str(path)) as connection:
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],"0058_reconnect_recovery_kit")
                self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"],"cleaning")

    def test_stop_protection_refuses_downgrade_instead_of_resurrecting_old_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            self.assertEqual(run_alembic(path, "0059_delayed_cloud_deletion").returncode,0)
            with PostgresConnection(str(path)) as connection:
                connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (1,'test','Test','/sources/test','bucket','vaults/test/')")
                connection.execute("""INSERT INTO protection_stops(vault_id,requested_at,cloud_disposition,reason)
                    VALUES (1,'2026-11-05T00:00:00+00:00','keep','stop')""")
            result = run_alembic(path,"0058_reconnect_recovery_kit",command="downgrade")
            self.assertNotEqual(result.returncode,0)
            self.assertIn("Cannot downgrade after stopping", result.stderr)
            with PostgresConnection(str(path)) as connection:
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0059_delayed_cloud_deletion")
                stop = connection.execute("SELECT cloud_disposition,reason FROM protection_stops").fetchone()
                self.assertEqual(stop, {"cloud_disposition": "keep", "reason": "stop"})
