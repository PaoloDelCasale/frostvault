"""#459 remaining acceptance through real admission/claim/worker on CI PG16.

Expected evidence is derived independently from fixture bytes/local snapshots;
provider observations never manufacture ownership or expected content.
"""
import hashlib
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import notifications, upload_remediation as remediation
from app.services import upload_remediation_discovery as discovery
from app.services.upload_snapshot import prepare_upload_snapshot
from app.services.vault_relocation import enroll_vault_root_identity
from tests import test_upload_remediation as previous

A, B = previous.fixtures.A, previous.fixtures.B


class UploadRemediationFrontierTests(unittest.TestCase):
    setUp = previous.UploadRemediationTests.setUp
    queue = previous.UploadRemediationTests.queue
    run_job = previous.UploadRemediationTests.run_job
    row = previous.UploadRemediationTests.row
    db = previous.UploadRemediationTests.db
    admit = previous.UploadRemediationTests.admit
    fail_after_ack = previous.UploadRemediationTests.fail_after_ack
    assert_keeper = previous.UploadRemediationTests.assert_keeper
    http_client = previous.UploadRemediationTests.http_client
    scan = previous.fixtures.UploadProvenanceWorkerTests.scan

    def seed_owned(self, *, vault_id=2, path="report.txt", snapshot=True):
        root = self.source if vault_id == 2 else self.root / f"vault-{vault_id}"
        root.mkdir(exist_ok=True)
        source = root / path
        source.write_bytes(A)
        expected, _ = prepare_upload_snapshot(source, lambda: None)
        stamp = storage.now_iso()
        prefix = "docs" if vault_id == 2 else f"vault-{vault_id}"
        provider_id = f"owned-{uuid.uuid4().hex}"
        self.provider.add(f"{prefix}/{path}", A, version=provider_id)
        with self.db() as connection:
            if vault_id != 2:
                connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
                    VALUES (%s,%s,%s,%s,'bucket',%s) ON CONFLICT DO NOTHING""",
                    (vault_id, prefix, prefix, str(root), prefix))
                enroll_vault_root_identity(connection, vault_id, str(root))
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(vault_id=vault_id,path=path,file_type="regular",
                size=len(A),mtime_ns=source.stat().st_mtime_ns,observed_at=stamp)
            av = catalog.record_archive_version(vault_id=vault_id,vault_file_id=file_id,path=path,
                object_key=f"{prefix}/{path}",provider_version_id=provider_id,size=len(A),
                storage_class="STANDARD",etag="not-proof",uploaded_at=stamp,observed_at=stamp,
                scan_id=stamp,origin="upload")
            if snapshot:
                catalog.set_version_upload_snapshot(av,snapshot_json=expected.as_json(),requested="s3_checksum")
        return av, file_id

    def completion_gap(self):
        original = self.queue()
        real = storage.set_job
        def before_completion(job_id, status, *args, **kwargs):
            if status == "completed":
                raise RuntimeError("injected completion boundary")
            return real(job_id, status, *args, **kwargs)
        with patch("app.storage.set_job", side_effect=before_completion):
            self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        with self.db() as connection:
            self.assertTrue(connection.execute("SELECT settled FROM upload_intents").fetchone()["settled"])
            self.assertIsNotNone(connection.execute("SELECT completed_at FROM snapshot_cleanup_inventories").fetchone()["completed_at"])
        return original

    def test_owned_unverified_without_any_failed_original_is_admitted_without_put(self):
        av, _ = self.seed_owned()
        with patch("app.storage.s3_client", side_effect=AssertionError("Discovery must be DB-only")):
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_originals").fetchone()["n"], 0)
        self.run_job(row["current_job_id"])
        self.assertEqual(self.assert_keeper(row["current_job_id"])["id"], av)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_legacy_unverified_with_completed_independent_digest_uses_exact_readback(self):
        av, file_id = self.seed_owned(snapshot=False)
        with self.db() as connection:
            original = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
                action,status,requested_by,requested_at,updated_at,upload_plaintext_sha256,upload_verification_requested)
                VALUES (2,%s,%s,'report.txt','upload','completed',1,%s,%s,%s,'read_back') RETURNING id""",
                (file_id,av,storage.now_iso(),storage.now_iso(),hashlib.sha256(A).hexdigest())).fetchone()["id"]
            discovery.capture(connection,storage.now_iso(),version_id=av)
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
        self.run_job(resumed)
        self.assertEqual(self.assert_keeper(resumed)["verification_method"], "read_back")
        self.assertEqual(self.row(original)["status"], "completed")
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,1,0))

    def test_send_started_facts_without_failed_original_resume_without_second_put(self):
        original = self.fail_after_ack()
        with self.db() as connection:
            # Populated legacy fixture: materialized intent, terminal original,
            # no failed-origin discovery. Do not change provider facts/evidence.
            connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (original,))
            connection.execute("DELETE FROM upload_remediation_discovery")
        before = self.row(original)
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_originals").fetchone()["n"], 0)
        self.run_job(resumed)
        self.assert_keeper(resumed)
        for key in ("status","updated_at","message","archive_version_id"):
            self.assertEqual(self.row(original)[key], before[key])
        self.assertEqual(self.provider.puts, 1)

    def test_unknown_unverified_discovery_never_creates_expected_evidence_or_io(self):
        self.provider.add("docs/report.txt",B,version="unknown-B")
        self.scan()
        before = dict(self.provider.objects)
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions WHERE provider_version_id='unknown-B'").fetchone()["integrity"], "unverified")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_scan_only_cleanup_of_verified_success_has_its_own_automatic_episode(self):
        original = self.queue(); self.run_job(original)
        keeper = self.assert_keeper(original)
        before = self.row(original)
        self.provider.add("docs/report.txt",A,version="scan-duplicate")
        self.scan()
        self.assertEqual(self.provider.deletes, 0)
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_originals").fetchone()["n"], 0)
        self.run_job(row["current_job_id"])
        self.assertEqual(self.assert_keeper(row["current_job_id"])["id"], keeper["id"])
        self.assertEqual(self.row(original), before)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))

    def test_later_manual_cleanup_keeps_previous_resolution_and_late_notifications(self):
        original = self.fail_after_ack()
        first = self.admit(); self.run_job(first); self.assert_keeper(first)
        with self.db() as connection:
            history = connection.execute("SELECT * FROM upload_remediations").fetchone()
        self.provider.add("docs/report.txt",A,version="later-duplicate")
        self.scan()
        second = self.admit()  # Reproduces the former AV unique violation.
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT * FROM upload_remediations WHERE id=%s", (history["id"],)).fetchone(), history)
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"], 2)
            connection.execute("DELETE FROM notification_deliveries")
            connection.execute("DELETE FROM notifications")
            notifications.enqueue_job_terminal_notification(connection,job_id=original)
            late = connection.execute("SELECT problem_status,read_at FROM notifications WHERE job_id=%s", (original,)).fetchone()
            self.assertEqual(late["problem_status"], "resolved")
            self.assertIsNotNone(late["read_at"])
        self.run_job(second); self.assert_keeper(second)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))

    def test_intent_and_verified_reuse_originals_deduplicate_exact_av(self):
        original = self.completion_gap()
        with self.db() as connection:
            av = connection.execute("SELECT * FROM archive_versions").fetchone()
            other = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
                action,status,requested_by,requested_at,updated_at,upload_plaintext_sha256,upload_snapshot_json)
                VALUES (2,%s,%s,'report.txt','upload','failed',1,%s,%s,%s,%s) RETURNING id""",
                (self.file_id,av["id"],storage.now_iso(),storage.now_iso(),av["plaintext_sha256"],av["upload_snapshot_json"])).fetchone()["id"]
            remediation.capture_failure(connection,other,storage.now_iso())
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"], 1)
            self.assertEqual({r["original_job_id"] for r in connection.execute("SELECT original_job_id FROM upload_remediation_originals").fetchall()}, {original,other})
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
        self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_wrong_original_proof_is_durably_rejected_not_linked_to_resolved_a(self):
        self.fail_after_ack()
        first = self.admit(); self.run_job(first); keeper = self.assert_keeper(first)
        with self.db() as connection:
            wrong = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
                action,status,requested_by,requested_at,updated_at,upload_plaintext_sha256)
                VALUES (2,%s,%s,'report.txt','upload','failed',1,%s,%s,%s) RETURNING id""",
                (self.file_id,keeper["id"],storage.now_iso(),storage.now_iso(),hashlib.sha256(B).hexdigest())).fetchone()["id"]
            remediation.capture_failure(connection,wrong,storage.now_iso())
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT rejected_reason FROM upload_remediation_discovery WHERE job_id=%s", (wrong,)).fetchone()["rejected_reason"], "proof_mismatch")
            self.assertIsNone(connection.execute("SELECT 1 FROM upload_remediation_originals WHERE original_job_id=%s", (wrong,)).fetchone())
            projected = remediation.project_jobs(connection,[self.row(wrong)])[0]
            self.assertIsNone(projected["remediation"])
            self.assertEqual(projected["remediation_rejection"], "proof_mismatch")
        self.assertEqual(self.provider.puts, 1)

    def test_original_exhausted_budget_is_not_reset_by_first_discovery(self):
        original = self.fail_after_ack()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET retry_count=8 WHERE id=%s", (original,))
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual((row["state"],row["reason"],row["retry_count"]), ("blocked","retry_budget_exhausted",8))
        resumed = self.admit()
        self.assertEqual(self.row(resumed)["retry_count"], 8)
        self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_round_robin_discovers_multiple_vault_batches_without_provider_io(self):
        for vault_id in range(2, 14):
            self.seed_owned(vault_id=vault_id)
        with patch.object(remediation,"VAULT_BATCH",2), patch.object(remediation,"TARGET_BATCH",1), patch("app.storage.s3_client",side_effect=AssertionError("Sweep cannot construct provider")):
            for tick in range(6):
                stamp = (datetime.now(timezone.utc)+timedelta(seconds=11*tick)).isoformat()
                self.assertEqual(remediation.sweep(self.db,stamp), 2)
        with self.db() as connection:
            self.assertEqual({r["vault_id"] for r in connection.execute("SELECT vault_id FROM upload_remediations").fetchall()}, set(range(2,14)))
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_jobs").fetchone()["n"], 12)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_locked_vault_does_not_starve_next_vault_or_lose_its_facts(self):
        self.seed_owned(); self.seed_owned(vault_id=3)
        with patch.object(remediation,"VAULT_BATCH",1):
            with self.db() as held:
                held.execute("SELECT id FROM vaults WHERE id=2 FOR UPDATE")
                self.assertEqual(remediation.sweep(self.db), 0)
                self.assertEqual(remediation.sweep(self.db), 1)
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual({r["vault_id"] for r in connection.execute("SELECT vault_id FROM upload_remediations").fetchall()}, {2,3})

    def test_timeslice_keeps_unprocessed_facts_for_next_tick(self):
        self.seed_owned(); self.seed_owned(path="other.txt")
        clock = [0.0]
        enroll = discovery.enroll
        def exhaust(*args, **kwargs):
            row = enroll(*args, **kwargs)
            clock[0] = 10.0
            return row
        with patch.object(remediation,"monotonic",side_effect=lambda:clock[0]), patch.object(discovery,"monotonic",side_effect=lambda:clock[0]), patch.object(discovery,"enroll",side_effect=exhaust):
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_facts").fetchone()["n"], 1)
        self.assertEqual(remediation.sweep(self.db), 1)

    def test_namespace_changed_after_admission_fails_before_provider_observation(self):
        self.seed_owned()
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
            connection.execute("UPDATE vaults SET s3_bucket='different-bucket' WHERE id=2")
        events = []
        self.provider.hook = lambda stage,**kwargs:events.append(stage)
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(events, [])
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_cleanup_checkpoint_survives_failure_before_completion_and_explicit_retry(self):
        self.fail_after_ack()
        resumed = self.admit()
        real = storage.set_job
        def fault(job_id,status,*args,**kwargs):
            if status == "completed":
                raise RuntimeError("injected before completion")
            return real(job_id,status,*args,**kwargs)
        with patch("app.storage.set_job",side_effect=fault):
            self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "failed")
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual(row["phase"], "completion")
            self.assertIsNotNone(connection.execute("SELECT completed_at FROM snapshot_cleanup_inventories WHERE job_id=%s", (row["cleanup_inventory_job_id"],)).fetchone()["completed_at"])
        final = self.admit(); self.run_job(final); self.assert_keeper(final)
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual((self.provider.puts,self.provider.gets),(1,0))

    def test_completion_commit_abort_retries_only_db_and_resolves_atomically(self):
        self.fail_after_ack(); resumed = self.admit()
        with self.db() as connection:
            connection.execute("CREATE SEQUENCE remediation_commit_attempt")
            connection.execute("""CREATE FUNCTION abort_remediation_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.status='completed' AND NEW.upload_reconcile_only
                    AND nextval('remediation_commit_attempt')=1 THEN
                    RAISE EXCEPTION 'injected completion commit' USING ERRCODE='40001';
                END IF; RETURN NEW; END $$""")
            connection.execute("""CREATE CONSTRAINT TRIGGER abort_remediation_completion AFTER UPDATE ON jobs
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION abort_remediation_commit()""")
        self.run_job(resumed); self.assert_keeper(resumed)
        with self.db() as connection:
            row = connection.execute("SELECT state,resolved_by_job_id FROM upload_remediations").fetchone()
            self.assertEqual(row, {"state":"resolved","resolved_by_job_id":resumed})
            self.assertGreaterEqual(connection.execute("SELECT last_value FROM remediation_commit_attempt").fetchone()["last_value"], 2)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,0,0))

    def test_cancel_during_duplicate_read_suppresses_restart_and_all_delete(self):
        self.provider.add("docs/report.txt",A,version="old-duplicate")
        self.fail_after_ack(); resumed = self.admit()
        def cancel(stage, **kwargs):
            if stage == "get":
                storage.cancel_jobs([resumed])
        self.provider.hook = cancel
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "cancelled")
        self.provider.hook = lambda *args,**kwargs:None
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT state FROM upload_remediations").fetchone()["state"], "cancelled")
        self.assertEqual(len(self.provider.objects), 2)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,0))

    def test_delete_commit_gap_resumes_same_job_without_get_put_or_second_delete(self):
        self.provider.add("docs/report.txt",A,version="old-duplicate")
        self.fail_after_ack(); resumed = self.admit()
        def die(stage, **kwargs):
            if stage == "after_delete":
                raise KeyboardInterrupt("process death after exact delete")
        self.provider.hook = die
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(resumed)
        self.provider.hook = lambda *args,**kwargs:None
        with self.db() as connection:
            connection.execute("UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s", (resumed,))
        storage.reconcile_interrupted_jobs()
        self.assertEqual(remediation.sweep(self.db), 0)
        self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))


if __name__ == "__main__":
    unittest.main()
