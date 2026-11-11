"""Durability, lifecycle and cancellation on disposable PostgreSQL16 (PR CI)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import time
import uuid
from unittest.mock import patch

from app import main
from app.catalog import ArchiveCatalog
from app.services import bulk_upload_admission as bulk, protection_stop, vault_relocation
from app.services.operation_policies import OperationPolicy, set_policy
from app.services.upload_activity import upload_activity
from tests.postgres import run_alembic
from tests.test_job_queue_fairness import QueueFixture
from tests import test_upload_provenance_worker as provenance_fixtures
from tests.postgres import PostgresConnection
import unittest


class BulkAdmissionProvenanceTests(unittest.TestCase):
    setUp = provenance_fixtures.UploadProvenanceWorkerTests.setUp
    queue = provenance_fixtures.UploadProvenanceWorkerTests.queue
    run_job = provenance_fixtures.UploadProvenanceWorkerTests.run_job
    row = provenance_fixtures.UploadProvenanceWorkerTests.row
    assert_keeper = provenance_fixtures.UploadProvenanceWorkerTests.assert_keeper

    def admit(self):
        with PostgresConnection(self.db_path) as c:
            request = bulk.create(c, vault_id=2, path="", user_id=1)
        with PostgresConnection(self.db_path) as c:
            bulk.advance(c, vault_id=2, group_id=request["group_id"])
            return c.execute("SELECT * FROM jobs WHERE group_id=%s", (request["group_id"],)).fetchone()

    def test_bulk_resumes_exact_ambiguous_send_without_a_second_put_or_changing_frozen_proof(self):
        def lost(stage, **kwargs):
            if stage == "accepted":
                raise RuntimeError("provider response lost")
        self.provider.hook = lost
        original = self.queue()
        self.run_job(original)
        self.assertEqual(self.provider.puts, 1)
        original_mode = self.row(original)["upload_verification_requested"]
        with PostgresConnection(self.db_path) as c:
            set_policy(c, 2, OperationPolicy(upload_verification="read_back" if original_mode == "s3_checksum" else "s3_checksum"))
        self.provider.hook = lambda *args, **kwargs: None
        resumed = self.admit()
        self.assertIsNotNone(resumed)
        self.assertTrue(resumed["upload_reconcile_only"])
        self.assertEqual(resumed["upload_verification_requested"], original_mode)
        self.assertEqual(resumed["origin"], "manual")
        self.run_job(resumed["id"])
        self.assert_keeper(resumed["id"])
        self.assertEqual(self.provider.puts, 1)

    def test_bulk_reconcile_only_cannot_put_while_an_incomplete_multipart_is_ambiguous(self):
        def crash(stage, **kwargs):
            if stage == "before_put":
                raise RuntimeError("worker died before request")
        self.provider.hook = crash
        original = self.queue()
        self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        self.provider.hook = lambda *args, **kwargs: None
        self.provider.multiparts.append(("docs/report.txt", "still-in-flight"))
        resumed = self.admit()
        self.assertTrue(resumed["upload_reconcile_only"])
        self.run_job(resumed["id"])
        self.assertIn("ambiguous_send", self.row(resumed["id"])["message"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))


class BulkAdmissionPostgresTests(QueueFixture):
    def seed(self, paths, vault_id=2):
        with self.db() as c:
            catalog = ArchiveCatalog(c)
            for path in paths:
                catalog.observe_local_copy(vault_id=vault_id, path=path, file_type="regular", size=12,
                                           mtime_ns=1, observed_at=self.clock.isoformat())

    def create(self, path="", vault_id=2, request_id=None):
        with self.db() as c:
            return bulk.create(c, vault_id=vault_id, path=path, user_id=1, request_id=request_id)

    def step(self, group_id, vault_id=2, size=2):
        with self.db() as c:
            return bulk.advance(c, vault_id=vault_id, group_id=group_id, batch_size=size)

    def status(self, group_id, vault_id=2):
        with self.db() as c:
            return bulk.get(c, vault_id=vault_id, group_id=group_id)

    def finish(self, group_id, vault_id=2):
        for _ in range(100):
            if self.status(group_id, vault_id)["status"] != "pending":
                return self.status(group_id, vault_id)
            self.step(group_id, vault_id)
        self.fail("producer did not exhaust its finite selection")

    def test_request_creation_is_db_only_idempotent_and_does_not_enqueue_files(self):
        self.seed(["a.jpg", "b.jpg", "c.jpg"])
        key = str(uuid.uuid4())
        with patch("app.services.upload_remediation.intercept_upload", side_effect=AssertionError("per-file work in HTTP")):
            first = self.create(request_id=key)
            self.assertEqual(self.create(request_id=key), first)
        self.assertEqual(first["selected_count"], 3)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(self.jobs(), [])
        with self.assertRaises(ValueError):
            self.create("different", request_id=key)
        with self.assertRaises(ValueError):
            self.create(vault_id=3, request_id=key)
        self.finish(key)
        self.assertEqual(self.create(request_id=key)["status"], "completed")
        self.assertEqual(len(self.jobs()), 3)

    def test_atomic_batches_restart_and_shrinking_eligibility_never_skip_or_duplicate(self):
        self.seed([f"p/{i}.jpg" for i in range(7)])
        group = self.create()["group_id"]
        # Simulated crash after writes, before commit: both cursor and Jobs vanish.
        with self.assertRaisesRegex(RuntimeError, "crash"):
            with self.db() as c:
                bulk.advance(c, vault_id=2, group_id=group, batch_size=2)
                raise RuntimeError("crash")
        self.assertEqual(self.status(group)["processed_count"], 0)
        self.assertEqual(self.jobs(), [])
        self.step(group)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.status(group)["processed_count"], 2)
        # Completed Jobs no longer exclude their files. A shrinking-candidate
        # OFFSET or replay from zero would re-admit them or miss later paths.
        with self.db() as c:
            c.execute("UPDATE jobs SET status='completed' WHERE group_id=%s", (group,))
        self.step(group)
        final = self.finish(group)
        self.assertEqual((final["processed_count"], final["admitted_count"], final["total_bytes"]), (7, 7, 84))
        jobs = self.jobs()
        self.assertEqual(len(jobs), len({j["vault_file_id"] for j in jobs}))
        self.assertEqual({j["group_id"] for j in jobs}, {group})
        self.assertTrue(all(j["origin"] == "manual" for j in jobs))
        self.assertFalse(self.step(group))

    def test_inactive_path_candidates_consume_the_batch_window_without_admitting_jobs(self):
        self.seed(["inactive-a.jpg", "inactive-b.jpg", "active.jpg"])
        with self.db() as c:
            c.execute("""UPDATE vault_files SET status='retired' WHERE id IN (
                SELECT vault_file_id FROM file_paths WHERE vault_id=2
                AND path IN ('inactive-a.jpg','inactive-b.jpg'))""")
        group = self.create()["group_id"]
        self.assertEqual(self.status(group)["selected_count"], 3)
        for count in (1, 2):
            self.step(group, size=1)
            self.assertEqual(self.status(group)["processed_count"], count)
            self.assertEqual(self.jobs(), [])
        self.finish(group)
        self.assertEqual([job["path"] for job in self.jobs()], ["active.jpg"])

    def test_watermark_does_not_follow_rename_or_new_identity_and_scope_is_literal(self):
        self.seed(["foo%_/a.jpg", "foo%_/b.jpg", "fooXX/a.jpg", "foobar/a.jpg"])
        group = self.create("foo%_")["group_id"]
        with self.db() as c:
            row = c.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=2 AND path='foo%_/b.jpg'").fetchone()
            c.execute("UPDATE file_paths SET valid_to=%s WHERE vault_file_id=%s", (self.clock.isoformat(), row["vault_file_id"]))
            c.execute("INSERT INTO file_paths(vault_file_id,vault_id,path,valid_from) VALUES (%s,2,%s,%s)", (row["vault_file_id"], "foo%_/renamed.jpg", self.clock.isoformat()))
        self.seed(["foo%_/new.jpg", "foo%_/b.jpg"])
        result = self.finish(group)
        self.assertEqual(result["processed_count"], result["selected_count"])
        self.assertEqual([j["path"] for j in self.jobs()], ["foo%_/a.jpg"])

    def test_eligibility_revalidated_and_remediation_arbiter_is_not_bypassed(self):
        self.seed(["a.jpg", "b.jpg", "c.jpg", "d.jpg"])
        group = self.create()["group_id"]
        with self.db() as c:
            c.execute("UPDATE local_copies SET presence='missing' WHERE vault_file_id=(SELECT vault_file_id FROM file_paths WHERE vault_id=2 AND path='b.jpg')")
            c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at)
                SELECT 2,vault_file_id,path,'recover','queued',%s,%s FROM file_paths WHERE vault_id=2 AND path='c.jpg'""",
                (self.clock.isoformat(), self.clock.isoformat()))
        from app.services import upload_remediation
        original = upload_remediation.intercept_upload
        seen = []
        def arbiter(c, **kwargs):
            seen.append(kwargs)
            path = c.execute("SELECT path FROM file_paths WHERE vault_file_id=%s AND valid_to IS NULL", (kwargs["file_id"],)).fetchone()["path"]
            if path == "d.jpg":
                return True, None  # unresolved/cancelled ambiguity forbids a new ordinary Job
            return original(c, **kwargs)
        with patch.object(upload_remediation, "intercept_upload", side_effect=arbiter):
            result = self.finish(group)
        self.assertEqual(result["admitted_count"], 1)
        self.assertEqual({j["path"] for j in self.jobs() if j["action"] == "upload"}, {"a.jpg"})
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(not call["automatic"] and call["group_id"] == group for call in seen))

    def test_frozen_policy_manual_origin_and_pause_does_not_prevent_admission_or_allow_claim(self):
        self.seed(["a.jpg", "b.jpg", "c.jpg"])
        with self.db() as c:
            set_policy(c, 2, OperationPolicy(upload_verification="read_back"))
        group = self.create()["group_id"]
        self.step(group)
        with self.db() as c:
            set_policy(c, 2, OperationPolicy(upload_verification="s3_checksum"))
            c.execute("UPDATE vaults SET paused=TRUE WHERE id=2")
        self.finish(group)
        jobs = self.jobs()
        self.assertEqual({j["upload_verification_requested"] for j in jobs}, {"read_back"})
        self.assertEqual({j["origin"] for j in jobs}, {"manual"})
        with self.db() as c:
            self.assertIsNone(ArchiveCatalog(c).claim_job(job_id=jobs[0]["id"], claim_token="held",
                claimed_at=self.clock.isoformat(), claim_expires_at=(self.clock + timedelta(hours=1)).isoformat(),
                now=self.clock.isoformat(), restore_due_before=self.clock.isoformat()))

    def test_group_cancel_before_any_job_and_between_batches_stops_future_production(self):
        self.seed([f"a/{n}.jpg" for n in range(5)])
        for after_batch in (False, True):
            with self.subTest(after_batch=after_batch):
                group = self.create()["group_id"]
                if after_batch:
                    self.step(group)
                with patch.object(main, "db", self.db):
                    result = main.cancel_job_group(group, "upload", self.vaults[2], 1)
                self.assertEqual(result["stopped_admissions"], 1)
                count = len(self.jobs())
                self.assertFalse(self.step(group))
                self.assertEqual(len(self.jobs()), count)
                self.assertEqual(self.create(request_id=group)["status"], "cancelled")

    def test_folder_cancel_pending_excludes_future_paths_without_stopping_siblings(self):
        self.seed(["first/a.jpg", "foo/a.jpg", "foo/sub/b.jpg", "foobar/a.jpg", "last/a.jpg"])
        group = self.create()["group_id"]
        self.step(group, size=1)
        with patch.object(main, "db", self.db):
            result = main.cancel_pending_uploads(main.PendingUploadCancelAction(path="foo"), {"id": 1}, self.vaults[2])
        self.assertEqual(result["cancelled_count"], 0)
        self.assertEqual(self.finish(group)["admitted_count"], 3)
        self.assertEqual({j["path"] for j in self.jobs()}, {"first/a.jpg", "foobar/a.jpg", "last/a.jpg"})

    def test_whole_cancel_pending_stops_producer_and_preserves_ambiguous_jobs(self):
        self.seed([f"{n}.jpg" for n in range(5)])
        group = self.create()["group_id"]
        self.step(group)
        jobs = self.jobs()
        self.add_intent(jobs[0]["id"], send_started=True)
        with patch.object(main, "db", self.db):
            result = main.cancel_pending_uploads(main.PendingUploadCancelAction(), {"id": 1}, self.vaults[2])
        self.assertEqual((result["cancelled_count"], result["stopped_admissions"]), (1, 1))
        self.assertFalse(self.step(group))
        self.assertEqual([j["status"] for j in self.jobs()], ["queued", "cancelled"])

    def test_stop_and_reconnect_consider_unproduced_work_then_cancel_allows_stop(self):
        self.seed(["a.jpg"])
        group = self.create()["group_id"]
        with self.db() as c:
            self.assertTrue(vault_relocation._active_jobs(c, 2))
            with self.assertRaisesRegex(ValueError, "active Jobs"):
                protection_stop.stop_protection(c, vault_id=2, actor_user_id=1, cloud_disposition="keep", confirmation="Photos", reason="test stop")
            with self.assertRaises(vault_relocation.VaultRelocationError) as error:
                vault_relocation._reconnect_vault_root(c, vault_id=2, volume_alias="unused", relative_path="", actor_user_id=1, reason="test reconnect")
            self.assertEqual(error.exception.reason, "active_jobs")
            bulk.cancel(c, vault_id=2, group_id=group)
            self.assertFalse(vault_relocation._active_jobs(c, 2))
            result = protection_stop.stop_protection(c, vault_id=2, actor_user_id=1, cloud_disposition="keep", confirmation="Photos", reason="test stop")
            self.assertTrue(result["stopped"])
        self.assertFalse(self.step(group))
        self.assertEqual(self.create(request_id=group)["status"], "cancelled")

    def test_lifecycle_and_identity_drift_invalidate_before_next_batch(self):
        self.seed(["a.jpg", "b.jpg", "c.jpg"])
        for field, value in (("root_identity", "different"), ("relocation_state", "scan_required")):
            group = self.create()["group_id"]
            self.step(group, size=1)
            with self.db() as c:
                c.execute(f"UPDATE vaults SET {field}=%s WHERE id=2", (value,))
            before = len(self.jobs())
            self.step(group)
            self.assertEqual(self.status(group)["status"], "invalidated")
            self.assertEqual(len(self.jobs()), before)
            with self.db() as c:
                c.execute(f"UPDATE vaults SET {field}=%s WHERE id=2", (self.vaults[2][field],))

    def test_busy_vault_does_not_block_other_vault_and_concurrent_worker_cannot_duplicate(self):
        self.seed(["a.jpg", "b.jpg", "c.jpg"])
        self.seed(["other.jpg"], vault_id=3)
        group = self.create()["group_id"]
        other = self.create(vault_id=3)["group_id"]
        with self.db() as held:
            self.assertTrue(bulk.advance(held, vault_id=2, group_id=group, batch_size=1))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(bulk.tick, self.db).result(timeout=5), 1)
        self.assertEqual(self.status(other, 3)["status"], "completed")
        self.finish(group)
        self.assertEqual(len(self.jobs()), 4)

    def test_failed_batch_rolls_back_without_preventing_other_vaults_from_advancing(self):
        self.seed(["a.jpg"])
        self.seed(["b.jpg"], vault_id=3)
        first = self.create()["group_id"]
        second = self.create(vault_id=3)["group_id"]
        advance = bulk.advance
        def fail_one(c, **kwargs):
            result = advance(c, **kwargs)
            if kwargs["vault_id"] == 2:
                raise RuntimeError("batch interrupted")
            return result
        with patch.object(bulk, "advance", side_effect=fail_one), self.assertRaisesRegex(RuntimeError, "interrupted"):
            bulk.tick(self.db)
        self.assertEqual(self.status(first)["processed_count"], 0)
        self.assertEqual(self.status(second, 3)["status"], "completed")
        self.assertEqual(len(self.jobs()), 1)
        self.finish(first)
        self.assertEqual(len(self.jobs()), 2)

    def test_progress_is_distinct_from_transfers_and_migration_refuses_pending_downgrade(self):
        self.seed(["a.jpg", "b.jpg", "c.jpg"])
        group = self.create()["group_id"]
        with self.db() as c:
            activity = upload_activity(c)["global"]
            self.assertEqual(activity["bulk_admission"], {"requests": 1, "selected_files": 3, "processed_files": 0, "admitted_files": 0})
            self.assertEqual(activity["uploading"]["transferred_bytes"], 0)
        result = run_alembic(self.database, "0070_vault_pause", "downgrade")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending bulk upload", result.stderr)
        self.finish(group)
        self.assertEqual(run_alembic(self.database, "0070_vault_pause", "downgrade").returncode, 0)
        result = run_alembic(self.database)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.jobs()), 3)

    def test_ten_thousand_paths_return_without_per_file_work_and_each_transaction_is_bounded(self):
        with self.db() as c:
            c.execute("""INSERT INTO vault_files(id,vault_id,created_at)
                SELECT 'bulk550-' || n,2,%s FROM generate_series(1,10000) n""", (self.clock.isoformat(),))
            c.execute("""INSERT INTO file_paths(vault_file_id,vault_id,path,valid_from)
                SELECT id,2,id,%s FROM vault_files WHERE vault_id=2 ORDER BY id""", (self.clock.isoformat(),))
            c.execute("""INSERT INTO local_copies(vault_file_id,presence,file_type,size,mtime_ns,observed_at)
                SELECT id,'present','regular',12,1,%s FROM vault_files WHERE vault_id=2""", (self.clock.isoformat(),))
        started = time.monotonic()
        with self.db() as c:
            request = bulk.create(c, vault_id=2, path="", user_id=1)
        elapsed = time.monotonic() - started
        print(f"#550 acceptance: 10000 path candidates / {elapsed:.3f} s", flush=True)
        self.assertLess(elapsed, 5)
        self.assertEqual(request["selected_count"], 10000)
        self.assertEqual(self.jobs(), [])
        with self.db() as c:
            bulk.advance(c, vault_id=2, group_id=request["group_id"])
        self.assertEqual(len(self.jobs()), bulk.BATCH_SIZE)
        self.assertEqual(self.status(request["group_id"])["processed_count"], bulk.BATCH_SIZE)
        # Another session can update the Vault between batches, not after 10k Jobs.
        with self.db() as c:
            c.execute("SET LOCAL lock_timeout='1s'")
            c.execute("UPDATE vaults SET name=name WHERE id=2")
