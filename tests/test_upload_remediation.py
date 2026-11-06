"""Actual admission/claim/worker and inbox projections on disposable PG16."""
import unittest
from unittest.mock import patch

from app import storage
from app.services import upload_remediation as remediation, notifications
from tests import test_upload_provenance_worker as fixtures
from tests.postgres import PostgresConnection


class UploadRemediationTests(unittest.TestCase):
    setUp = fixtures.UploadProvenanceWorkerTests.setUp
    queue = fixtures.UploadProvenanceWorkerTests.queue
    run_job = fixtures.UploadProvenanceWorkerTests.run_job
    row = fixtures.UploadProvenanceWorkerTests.row
    assert_keeper = fixtures.UploadProvenanceWorkerTests.assert_keeper
    http_client = fixtures.UploadProvenanceWorkerTests.http_client

    def db(self):
        return PostgresConnection(self.db_path)

    def fail_after_ack(self):
        self.provider.hook = lambda stage, **kw: (_ for _ in ()).throw(RuntimeError("post raw ack fault")) if stage == "acknowledged" else None
        original = self.queue()
        self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        self.provider.hook = lambda *args, **kw: None
        return original

    def admit(self):
        with self.db() as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            return remediation.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())["job_id"]

    def test_post_ack_original_preserved_resolution_requires_completed_cleanup(self):
        original = self.fail_after_ack()
        before = self.row(original)
        resumed = self.admit()
        with self.db() as connection:
            notifications.reconcile_problem_states(connection)
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE job_id=%s", (original,)).fetchone()["problem_status"], "open")
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts,1)
        after = self.row(original)
        for key in ("status","message","requested_at","updated_at","group_id"):
            self.assertEqual(after[key],before[key],key)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual((row["state"],row["resolved_by_job_id"]),("resolved",resumed))
            notifications.reconcile_problem_states(connection)
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE job_id=%s", (original,)).fetchone()["problem_status"],"resolved")
            connection.execute("DELETE FROM notification_deliveries")
            connection.execute("DELETE FROM notifications")
            notifications.enqueue_job_terminal_notification(connection,job_id=original)
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE job_id=%s",(original,)).fetchone()["problem_status"],"resolved","Late delivery cannot reopen resolved history")

    def test_verified_cleanup_fault_is_resumable_without_put_or_readback(self):
        original = self.queue()
        with patch("app.storage.purge_current_snapshot_superseded",side_effect=RuntimeError("post verified fault")):
            self.run_job(original)
        self.assertEqual(self.row(original)["status"],"failed")
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions").fetchone()["integrity"],"verified")
        resumed = self.admit()
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts,self.provider.gets),(1,0))

    def test_failed_remediation_never_rediscovered_as_fresh_original(self):
        original = self.fail_after_ack()
        resumed = self.admit()
        self.provider.hook = lambda stage, **kw: (_ for _ in ()).throw(RuntimeError("permanent HEAD fault")) if stage == "head" else None
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"],"failed")
        for _ in range(3):
            remediation.sweep(self.db)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM jobs").fetchone()["n"],2)
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"],1)
            self.assertEqual(connection.execute("SELECT state FROM upload_remediations").fetchone()["state"],"blocked")
            self.assertEqual(connection.execute("SELECT original_job_id FROM upload_remediation_originals").fetchone()["original_job_id"],original)
        self.assertEqual(self.provider.puts,1)

    def test_cancelled_remediation_persists_target_suppression(self):
        self.fail_after_ack()
        resumed = self.admit()
        storage.set_job(resumed,"cancelled","owner cancelled")
        for _ in range(3):
            remediation.sweep(self.db)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT state FROM upload_remediations").fetchone()["state"],"cancelled")
            self.assertEqual(connection.execute("SELECT count(*) n FROM jobs").fetchone()["n"],2)
        with self.assertRaisesRegex(Exception,"identity_changed"):
            self.admit()
        self.assertEqual(self.provider.puts,1)

    def test_queued_failed_and_completed_b_do_not_resolve_failed_a(self):
        original = self.fail_after_ack()
        with self.db() as connection:
            for status in ("queued","failed","completed"):
                other = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at)
                    VALUES (2,%s,'report.txt','upload',%s,1,%s,%s) RETURNING id""",(self.file_id,status,storage.now_iso(),storage.now_iso())).fetchone()["id"]
                notifications.reconcile_problem_states(connection)
                self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE job_id=%s",(original,)).fetchone()["problem_status"],"open",status)
                connection.execute("UPDATE jobs SET status='failed' WHERE id=%s",(other,))

    def test_verified_alone_cannot_complete_remediation(self):
        self.fail_after_ack()
        resumed = self.admit()
        with self.assertRaisesRegex(RuntimeError,"exact cleanup postcondition"):
            storage.set_job(resumed,"completed")
        self.assertEqual(self.row(resumed)["status"],"queued","Authoritative transition rolls back")


if __name__ == "__main__":
    unittest.main()
