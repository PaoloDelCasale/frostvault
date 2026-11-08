"""Actual admission/claim/worker and inbox projections on disposable PG16."""
import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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

    def test_interrupted_own_request_finishing_during_inventory_auto_reconciles(self):
        original = self.fail_after_ack()
        version, payload = next(iter(self.provider.objects.items()))
        del self.provider.objects[version]
        # Replay an old in-flight completion, not a second PUT or a new baseline.
        listings = []
        def finish(stage,**kwargs):
            if stage == 'list':
                listings.append(1)
                if len(listings) == 2:
                    self.provider.objects[version] = payload
        self.provider.hook = finish
        self.assertEqual(remediation.sweep(self.db),1)
        with self.db() as connection:
            resumed = connection.execute('SELECT current_job_id FROM upload_remediations').fetchone()['current_job_id']
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts,1)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT state FROM upload_remediations').fetchone()['state'],'resolved')
            self.assertEqual(connection.execute('SELECT status FROM jobs WHERE id=%s',(original,)).fetchone()['status'],'failed')

    def test_interrupted_upload_with_external_inventory_delta_stays_blocked(self):
        self.fail_after_ack()
        listings = []
        def external(stage,**kwargs):
            if stage == 'list':
                listings.append(1)
                if len(listings) == 2:
                    self.provider.add('docs/report.txt',fixtures.A,version='external-writer')
        self.provider.hook = external
        self.assertEqual(remediation.sweep(self.db),1)
        with self.db() as connection:
            resumed = connection.execute('SELECT current_job_id FROM upload_remediations').fetchone()['current_job_id']
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)['status'],'failed')
        self.assertIn('Retry failed uploads',self.row(resumed)['message'])
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db),0)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT state,reason FROM upload_remediations').fetchone(),
                             {'state':'blocked','reason':'inventory_changed'})
        self.assertEqual((self.provider.puts,self.provider.deletes),(1,0))
        self.assertEqual(len(self.provider.objects),2)

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
        with self.assertRaisesRegex(Exception,"owner_cancelled"):
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

    def test_discovery_admission_continues_while_actual_provider_io_is_blocked(self):
        self.fail_after_ack()
        other = self.source / "other.txt"
        other.write_bytes(fixtures.A)
        with self.db() as connection:
            from app.catalog import ArchiveCatalog
            ArchiveCatalog(connection).observe_local_copy(vault_id=2,path="other.txt",file_type="regular",size=len(fixtures.A),mtime_ns=other.stat().st_mtime_ns,observed_at=storage.now_iso())
        self.provider.hook = lambda stage, **kw: (_ for _ in ()).throw(RuntimeError("post raw ack fault")) if stage == "acknowledged" else None
        fixtures.queue_jobs("other.txt","upload",2,1)
        with self.db() as connection:
            second = connection.execute("SELECT max(id) id FROM jobs").fetchone()["id"]
        self.run_job(second)
        self.assertEqual(self.row(second)["status"],"failed")
        self.provider.hook = lambda *args, **kw: None
        resumed = self.admit()
        entered,release = threading.Event(),threading.Event()
        def block(stage, **kw):
            if stage == "head" and kw["key"] == "docs/report.txt":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test provider I/O was not released")
        self.provider.hook = block
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.run_job,resumed)
            try:
                self.assertTrue(entered.wait(5),"Actual claimed worker must enter provider I/O")
                with patch("app.storage.s3_client",side_effect=AssertionError("Sweep cannot use provider")):
                    self.assertEqual(remediation.sweep(self.db),1)
                self.assertFalse(future.done(),"Admission must not wait for blocked provider I/O")
                with self.db() as connection:
                    self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_jobs").fetchone()["n"],2)
            finally:
                release.set()
            future.result(timeout=5)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts,2,"Discovery never creates provider versions")

    def test_sweep_is_db_only_and_admits_historical_verified_completion_failure(self):
        original = self.queue()
        with patch("app.storage.purge_current_snapshot_superseded",side_effect=RuntimeError("post verified fault")):
            self.run_job(original)
        with patch("app.storage.s3_client",side_effect=AssertionError("discovery cannot use provider")), patch("app.services.source_layout.vault_local_access",side_effect=AssertionError("discovery cannot probe filesystem")):
            self.assertEqual(remediation.sweep(self.db),1)
            self.assertEqual(remediation.sweep(self.db),0)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts,self.provider.gets),(1,0))

    def test_a_changed_to_b_is_not_protected_by_remediation_of_a(self):
        self.fail_after_ack()
        self.file.write_bytes(fixtures.B)
        resumed = self.admit()
        self.run_job(resumed)
        version = self.assert_keeper(resumed)
        self.assertEqual(version["plaintext_sha256"],hashlib.sha256(fixtures.A).hexdigest())
        with self.db() as connection:
            local = connection.execute("SELECT * FROM local_copies WHERE vault_file_id=%s",(self.file_id,)).fetchone()
            self.assertNotEqual(local["matched_archive_version_id"],version["id"])
        self.assertEqual(self.provider.puts,1)

    def test_target_budget_deadline_restart_and_explicit_retry_do_not_reset(self):
        original = self.fail_after_ack()
        resumed = self.admit()
        self.provider.hook = lambda stage, **kw: (_ for _ in ()).throw(TimeoutError("connection reset")) if stage == "head" else None
        self.run_job(resumed)
        self.assertEqual((self.row(resumed)["status"],self.row(resumed)["retry_count"]),("retrying",1))
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual((row["retry_count"],row["due_at"]),(1,self.row(resumed)["retry_after"]))
            epoch = row["id"]
            # Advance the durable fixture budget to its last permitted retry.
            connection.execute("UPDATE jobs SET retry_count=8,retry_after=%s WHERE id=%s",((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),resumed))
            connection.execute("UPDATE upload_remediations SET retry_count=8 WHERE id=%s",(epoch,))
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"],"failed")
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db),0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT reason FROM upload_remediations").fetchone()["reason"],"retry_budget_exhausted")
        self.provider.hook = lambda *args, **kw: None
        explicit = self.admit()
        self.assertEqual(self.row(explicit)["retry_count"],8,"Explicit retry keeps the target budget")
        self.run_job(explicit)
        self.assert_keeper(explicit)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT id FROM upload_remediations").fetchone()["id"],epoch)
        self.assertEqual(self.provider.puts,1)
        with self.db() as connection:
            notifications.reconcile_problem_states(connection)
            failures = connection.execute("SELECT job_id,problem_status,read_at FROM notifications WHERE event='job_failed'").fetchall()
            self.assertEqual({row["job_id"] for row in failures}, {original, resumed})
            self.assertTrue(all(row["problem_status"] == "resolved" and row["read_at"] for row in failures), "Failed remediation attempts are resolved and read with their exact target")
            connection.execute("DELETE FROM notification_deliveries")
            connection.execute("DELETE FROM notifications")
            notifications.enqueue_job_terminal_notification(connection, job_id=resumed)
            late = connection.execute("SELECT problem_status,read_at FROM notifications WHERE job_id=%s", (resumed,)).fetchone()
            self.assertEqual(late["problem_status"], "resolved")
            self.assertIsNotNone(late["read_at"], "Late resolved attempts must not remain unread forever")

    def test_concurrent_manual_and_sweep_share_one_target_and_active_file(self):
        self.fail_after_ack()
        def explicit():
            try:
                return self.admit()
            except remediation.provenance.UploadBlocked:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda f:f(),(explicit,lambda:remediation.sweep(self.db))))
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"],1)
            self.assertEqual(connection.execute("SELECT count(*) n FROM jobs WHERE status NOT IN ('failed','cancelled','completed')").fetchone()["n"],1)
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_jobs").fetchone()["n"],1)
        self.assertTrue(any(results))
        self.assertEqual(self.provider.puts,1)

    def test_other_live_and_expired_claims_are_not_absence_of_active_work(self):
        self.fail_after_ack()
        with self.db() as connection:
            other = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at,claim_token,claim_expires_at)
                VALUES (2,%s,'report.txt','recover','queued',1,%s,%s,'other-live-claim',%s) RETURNING id""",(self.file_id,storage.now_iso(),storage.now_iso(),storage._claim_expiry_at())).fetchone()["id"]
        self.assertEqual(remediation.sweep(self.db),0)
        future = (datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat()
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT reason FROM upload_remediations").fetchone()["reason"],"active_operation")
            connection.execute("UPDATE jobs SET claim_expires_at=%s WHERE id=%s",("2020-01-01T00:00:00+00:00",other))
        self.assertEqual(remediation.sweep(self.db,future),0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM jobs").fetchone()["n"],2,"Expired claim is still the same active operation")

    def test_source_volume_suspend_is_durable_but_single_file_missing_can_complete(self):
        from types import SimpleNamespace
        self.fail_after_ack()
        resumed = self.admit()
        with patch("app.services.source_layout.vault_local_access",return_value=SimpleNamespace(local_operations_allowed=False)):
            self.assertFalse(self.run_job(resumed))
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT state,reason FROM upload_remediations").fetchone(),{"state":"suspended","reason":"source_volume_unavailable"})
        self.assertEqual(remediation.sweep(self.db),0)
        self.file.unlink()
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT status,retry_count FROM jobs WHERE id=%s",(resumed,)).fetchone(),{"status":"retrying","retry_count":0})
            connection.execute("UPDATE jobs SET retry_after='2000-01-01T00:00:00+00:00' WHERE id=%s",(resumed,))
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts,1)

    def test_targeted_history_and_bidirectional_lookup_survive_recent50_window(self):
        original = self.fail_after_ack()
        resumed = self.admit()
        self.run_job(resumed)
        self.assert_keeper(resumed)
        with self.db() as connection:
            for _ in range(55):
                connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at)
                    VALUES (2,%s,'report.txt','upload','completed',1,%s,%s)""",(self.file_id,storage.now_iso(),storage.now_iso()))
        client = self.http_client()
        with patch("app.main._allowed_hosts",return_value=[]):
            recent = client.get("/api/jobs?vault_id=2")
            self.assertEqual(recent.status_code,200,recent.text)
            self.assertNotIn(original,[r["id"] for r in recent.json()["items"]])
            first = client.get("/api/files/jobs?vault_id=2&path=report.txt").json()
            second = client.get(f"/api/files/jobs?vault_id=2&path=report.txt&before_id={first['next_before_id']}").json()
            self.assertIn(original,[r["id"] for r in second["items"]])
            linked = client.get(f"/api/jobs/{original}?vault_id=2").json()
            self.assertEqual(linked["remediation"]["resolved_by_job_id"],resumed)
            reverse = client.get(f"/api/jobs/{resumed}?vault_id=2").json()
            self.assertIn(original,reverse["remediation"]["original_job_ids"])
            self.assertNotIn("claim_token",reverse)

    def test_verified_alone_cannot_complete_remediation(self):
        original = self.queue()
        with patch("app.storage.purge_current_snapshot_superseded", side_effect=RuntimeError("post verified fault")):
            self.run_job(original)
        resumed = self.admit()
        with self.db() as connection:
            linked = self.row(resumed)["archive_version_id"]
            self.assertIsNotNone(linked)
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions WHERE id=%s", (linked,)).fetchone()["integrity"], "verified")
            # Isolate ONLY the incomplete cleanup guard: linked verified AV,
            # settled intent and exact inventory pointer are all present.
            connection.execute("UPDATE upload_intents SET settled=TRUE WHERE archive_version_id=%s", (linked,))
            connection.execute("UPDATE snapshot_cleanup_inventories SET keeper_id=%s,completed_at=NULL WHERE job_id=%s", (linked, original))
            connection.execute("UPDATE upload_remediations SET cleanup_inventory_job_id=%s", (original,))
            self.assertTrue(connection.execute("SELECT settled FROM upload_intents WHERE archive_version_id=%s", (linked,)).fetchone()["settled"])
        with self.assertRaisesRegex(RuntimeError,"exact cleanup postcondition"):
            storage.set_job(resumed,"completed")
        self.assertEqual(self.row(resumed)["status"],"queued","Authoritative transition rolls back")


if __name__ == "__main__":
    unittest.main()
