"""#464 core acceptance through real admission/claim/process_job on disposable PG16.

TEST_POSTGRES_URL is mandatory; never skipped locally as database evidence.
Provider persists actual version bytes/Metadata across worker invocations.
"""
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.main import queue_jobs
from app.services import upload_provenance as provenance
from app.services.vault_relocation import enroll_vault_root_identity
from tests.postgres import PostgresConnection, database_dsn, run_alembic
from tests.upload_provenance_provider import VersionedProvider

A = b"immutable content A"
B = b"immutable content B"


class UploadProvenanceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "report.txt"
        self.file.write_bytes(A)
        self.db_path = self.root / "catalog"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.provider = VersionedProvider()
        with PostgresConnection(self.db_path) as connection:
            self.assertTrue(connection.execute("SHOW server_version_num").fetchone()["server_version_num"].startswith("16"))
            connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
            connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'docs','Docs',%s,'bucket','docs')", (str(self.source),))
            enroll_vault_root_identity(connection, 2, str(self.source))
            ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=len(A), mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())
            self.file_id = connection.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=2 AND path='report.txt' AND valid_to IS NULL").fetchone()["vault_file_id"]
        self.enterContext(patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(self.db_path))))
        self.enterContext(patch("app.storage.validate_cloud_vault"))
        self.enterContext(patch("app.storage.s3_client", return_value=self.provider))
        self.enterContext(patch("app.storage.upload_file", side_effect=self.provider.upload))

    def queue(self):
        queued = queue_jobs("report.txt", "upload", 2, 1)
        self.assertEqual(queued["item_count"], 1)
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]

    def run_job(self, job_id):
        with PostgresConnection(self.db_path) as connection:
            claimed = ArchiveCatalog(connection).claim_job(job_id=job_id, claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(), claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(), restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        return storage.process_job(job)

    def row(self, job_id):
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def resume(self):
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            result = provenance.schedule_resume(connection, vault=vault, path="report.txt", user_id=1, timestamp=storage.now_iso())
        self.run_job(result["job_id"])
        return result["job_id"]

    def assert_keeper(self, job_id):
        row = self.row(job_id)
        self.assertEqual(row["status"], "completed", row["message"])
        with PostgresConnection(self.db_path) as connection:
            version = connection.execute("SELECT * FROM archive_versions WHERE id=%s", (row["archive_version_id"],)).fetchone()
        self.assertEqual((version["integrity"], version["availability"]), ("verified", "available"))
        self.assertIn((version["object_key"], version["provider_version_id"]), self.provider.objects)
        return version

    def test_committed_intent_send_snapshot_and_token_precede_provider_mutation(self):
        def inspect(stage, **kwargs):
            if stage == "before_put":
                with PostgresConnection(self.db_path) as connection:
                    intent = connection.execute("SELECT * FROM upload_intents").fetchone()
                    self.assertTrue(intent["send_started"])
                    self.assertEqual(intent["snapshot_json"], kwargs["snapshot"].as_json())
                    self.assertEqual(kwargs["metadata"][provenance.TOKEN_KEY], intent["token"])
                    self.assertIsNotNone(connection.execute("SELECT job_id FROM snapshot_cleanup_inventories").fetchone())
        self.provider.hook = inspect
        job = self.queue()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.provider.gets, 0)

    def test_prewrite_commit_failure_never_sends(self):
        job = self.queue()
        def fail(connection, *args):
            raise RuntimeError("prewrite commit unavailable")
        with patch.object(provenance, "mark_send_started", side_effect=fail):
            self.run_job(job)
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.provider.objects, {})

    def test_provider_accepted_lost_response_reconciles_token_without_second_put(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "accepted" else None
        original = self.queue()
        self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM upload_receipts").fetchone()["n"], 0)
        resumed = self.resume()
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertEqual(self.row(original)["status"], "failed", "#459 owns historical resolution")

    def test_ack_survives_error_before_flush_head_linkage_and_commit(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("post raw ack fault")) if stage == "acknowledged" else None
        original = self.queue()
        self.run_job(original)
        self.assertIsNone(self.row(original)["archive_version_id"])
        with PostgresConnection(self.db_path) as connection:
            receipt = provenance.receipt(connection, connection.execute("SELECT id FROM upload_intents").fetchone()["id"])
            self.assertIn(("docs/report.txt", receipt["VersionId"]), self.provider.objects)
        # Source B and changed requested mode must not redefine A's expected proof.
        self.file.write_bytes(B)
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE vault_operation_policies SET upload_verification='read_back' WHERE vault_id=2")
        resumed = self.resume()
        keeper = self.assert_keeper(resumed)
        self.assertEqual(keeper["plaintext_sha256"], hashlib.sha256(A).hexdigest())
        with PostgresConnection(self.db_path) as connection:
            local = connection.execute("SELECT * FROM local_copies WHERE vault_file_id=%s", (self.file_id,)).fetchone()
        self.assertNotEqual(local["matched_archive_version_id"], keeper["id"])
        self.assertEqual(self.provider.puts, 1)

    def test_missing_individual_file_does_not_prevent_materialized_resume(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue()
        self.run_job(original)
        self.file.unlink()
        resumed = self.resume()
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_cancel_after_raw_ack_keeps_receipt_and_never_resurrects(self):
        job = self.queue()
        def cancel(stage, **kwargs):
            if stage == "acknowledged":
                storage.cancel_jobs([job])
        self.provider.hook = cancel
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "cancelled")
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
            self.assertFalse(provenance.describe(connection, vault_id=2, path="report.txt")["can_resume"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertIsNone(self.row(job)["archive_version_id"])

    def test_receipt_append_after_lease_loss_is_fact_only(self):
        job = self.queue()
        def takeover(stage, **kwargs):
            if stage == "accepted":
                with PostgresConnection(self.db_path) as connection:
                    connection.execute("UPDATE jobs SET claim_token='new-owner' WHERE id=%s", (job,))
        self.provider.hook = takeover
        self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
            self.assertIsNone(intent["archive_version_id"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))

    def test_send_started_zero_match_remains_reconcile_only(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("before provider request")) if stage == "before_put" else None
        job = self.queue()
        self.run_job(job)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, resumed)
            self.assertTrue(intent["send_started"])
            self.assertEqual(intent["blocked_reason"], "ambiguous_send")

    def test_multiple_tokens_never_choose_latest_or_delete(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "accepted" else None
        job = self.queue()
        self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
        self.provider.add("docs/report.txt", A, {provenance.TOKEN_KEY: intent["token"]}, version="second-token")
        before = dict(self.provider.objects)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))

    def test_historical_duplicate_adopted_without_promoting_current_snapshot(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        job = self.queue()
        self.run_job(job)
        keeper = self.assert_keeper(job)
        self.assertEqual(self.provider.deleted, [("docs/report.txt", "old-orphan")])
        self.assertEqual(self.provider.gets, 1)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM archive_versions").fetchone()["n"], 1)
            proof = connection.execute("SELECT * FROM snapshot_cleanup_proofs").fetchone()
            self.assertEqual(proof["keeper_id"], keeper["id"])
            self.assertEqual(proof["method"], "exact_get_duplicate")
            self.assertIsNotNone(proof["deleted_at"])

    def test_historical_different_or_unreadable_preserves_keeper_with_actionable_resume(self):
        self.provider.add("docs/report.txt", B, version="different-orphan")
        job = self.queue()
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            keeper = connection.execute("SELECT * FROM archive_versions WHERE id=%s", (self.row(job)["archive_version_id"],)).fetchone()
            state = provenance.describe(connection, vault_id=2, path="report.txt")
        self.assertEqual(keeper["integrity"], "verified")
        self.assertEqual(state["reason"], "orphan_different")
        self.assertTrue(state["can_resume"])
        self.assertEqual(len(self.provider.objects), 2)
        self.assertEqual(self.provider.deletes, 0)
        # Normal admission is still 409: exact resume is independently available.
        with self.assertRaises(Exception):
            self.queue()

    def test_db_transaction_and_commit_retry_never_replays_provider(self):
        for code in ("40P01", "40001"):
            with self.subTest(sqlstate=code):
                # Nontransactional sequence survives the failed COMMIT, unlike a
                # row-based counter which would fail forever after rollback.
                with PostgresConnection(self.db_path) as connection:
                    connection.execute(f"CREATE SEQUENCE fail_{code}")
                    connection.execute(f"""CREATE FUNCTION fail_receipt_{code}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('fail_{code}')=1 THEN RAISE EXCEPTION 'commit fault' USING ERRCODE='{code}'; END IF; RETURN NEW; END $$""")
                    connection.execute(f"CREATE CONSTRAINT TRIGGER receipt_commit_fault AFTER INSERT ON upload_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fail_receipt_{code}()")
                job = self.queue()
                puts_before = self.provider.puts
                self.run_job(job)
                self.assert_keeper(job)
                self.assertEqual(self.provider.puts - puts_before, 1)
                with PostgresConnection(self.db_path) as connection:
                    connection.execute("DROP TRIGGER receipt_commit_fault ON upload_receipts")
                    # A fresh local generation earns ordinary upload admission.
                    self.file.write_bytes(B if code == "40P01" else A)
                    ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=self.file.stat().st_size, mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())
