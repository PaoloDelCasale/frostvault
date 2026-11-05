"""Preserve destructive-group lease/restart/partial-failure regression coverage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import unittest
from unittest.mock import Mock, patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import cloud_deletion as deletion, protection_stop
from tests import test_cloud_deletion as fixtures
from tests.postgres import database_dsn

START, DUE = fixtures.START, fixtures.DUE


class CloudDeletionLeaseTests(unittest.TestCase):
    setUp = fixtures.CloudDeletionTests.setUp
    connection = fixtures.CloudDeletionTests.connection
    add_file = fixtures.CloudDeletionTests.add_file
    schedule = fixtures.CloudDeletionTests.schedule
    claim = fixtures.CloudDeletionTests.claim
    execute = fixtures.CloudDeletionTests.execute

    def test_two_schedulers_claim_entire_group_once(self):
        with self.connection() as connection:
            self.add_file(connection,"second.jpg")
            scheduled = self.schedule(connection,paths=["photo.jpg","second.jpg"])
        barrier = threading.Barrier(2)
        def claim(number):
            with self.connection() as connection:
                barrier.wait(timeout=20)
                return ArchiveCatalog(connection).claim_purge_group(
                    lead_job_id=scheduled.job_ids[number],claim_token=f"worker-{number}",
                    claimed_at=DUE.isoformat(),claim_expires_at=(DUE+timedelta(hours=1)).isoformat(),
                    now=DUE.isoformat(),message="delete",message_key="job.cloud_purge_deleting")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim,range(2)))
        self.assertEqual(sorted(len(r) for r in results),[0,2])
        with self.connection() as connection:
            rows = connection.execute("SELECT status,claim_token FROM jobs").fetchall()
        self.assertEqual({r["status"] for r in rows},{"cleaning"})
        self.assertEqual(len({r["claim_token"] for r in rows}),1)

    def test_cancellation_racing_due_claim_is_all_or_nothing(self):
        with self.connection() as connection:
            self.add_file(connection, "second.jpg")
            scheduled = self.schedule(connection, paths=["photo.jpg", "second.jpg"])
        barrier = threading.Barrier(2)
        def cancel():
            with self.connection() as connection:
                barrier.wait(timeout=20)
                try:
                    deletion.cancel_cloud_deletion(connection, vault_id=1, group_id=scheduled.group_id,
                        actor_user_id=1, cancelled_at=DUE.isoformat())
                    return True
                except deletion.CloudDeletionError:
                    return False
        def claim():
            with self.connection() as connection:
                barrier.wait(timeout=20)
                return ArchiveCatalog(connection).claim_purge_group(lead_job_id=scheduled.job_ids[0],
                    claim_token="worker", claimed_at=DUE.isoformat(),
                    claim_expires_at=(DUE+timedelta(hours=1)).isoformat(), now=DUE.isoformat(),
                    message="delete", message_key="job.cloud_purge_deleting")
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled, claimed = pool.submit(cancel), pool.submit(claim)
            cancel_won, rows = cancelled.result(timeout=30), claimed.result(timeout=30)
        with self.connection() as connection:
            states = {r["status"] for r in connection.execute("SELECT status FROM jobs").fetchall()}
        self.assertEqual(states, {"cancelled"} if cancel_won else {"cleaning"})
        self.assertEqual(len(rows), 0 if cancel_won else 2)

    def test_provider_partial_failure_preserves_remaining_copy_and_requires_new_delay(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
        client = Mock()
        # execute() supplies a default response; side_effect preserves this failure.
        client.delete_objects.side_effect = lambda **kwargs: {"Errors":[{
            "Key":"vaults/test/photo.jpg","VersionId":"photo.jpg-v1","Code":"AccessDenied"}]}
        self.execute(scheduled,client=client)
        with self.connection() as connection:
            rows = connection.execute("SELECT provider_version_id,availability FROM archive_versions ORDER BY version_number").fetchall()
            self.assertEqual([(r["provider_version_id"],r["availability"]) for r in rows],
                             [("photo.jpg-v0","purged"),("photo.jpg-v1","available")])
            self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"],"failed")
            next_request = self.schedule(connection,requested_at=DUE.isoformat())
            self.assertEqual(next_request.pending_until,(DUE+timedelta(days=7)).isoformat())
            targets = connection.execute("SELECT provider_version_id FROM cloud_deletion_items WHERE job_id=%s",(next_request.job_ids[0],)).fetchall()
            self.assertEqual([r["provider_version_id"] for r in targets],["photo.jpg-v1"])

    def test_restart_finalizes_catalogued_deleted_items_without_repeat_provider_call(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
            ids = [r["id"] for r in connection.execute("SELECT id FROM cloud_deletion_items").fetchall()]
            deletion.mark_items_deleted(connection,item_ids=ids,updated_at=DUE.isoformat())
            connection.execute("""UPDATE jobs SET status='cleaning',claim_token='dead-worker',
                claimed_at=%s,claim_expires_at='2000-01-01T00:00:00+00:00'""",(DUE.isoformat(),))
        with patch("app.database.settings",database_dsn=database_dsn(str(self.path))), \
             patch("app.storage.now_iso",return_value=DUE.isoformat()):
            result = storage.reconcile_interrupted_jobs()
        self.assertEqual(result,{"completed":0,"requeued":1,"failed":0})
        client = self.execute(scheduled)
        client.delete_objects.assert_not_called()
        with self.connection() as connection:
            row = connection.execute("SELECT status,claim_token FROM jobs").fetchone()
        self.assertEqual(row["status"],"completed")
        self.assertIsNone(row["claim_token"])

    def test_expired_claim_cannot_publish_provider_results_over_replacement_lease(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
        job = self.claim(scheduled)
        started, release = threading.Event(),threading.Event()
        errors = []
        client = Mock()
        def provider(**kwargs):
            started.set()
            if not release.wait(timeout=20): raise AssertionError("Provider not released")
            return {}
        client.delete_objects.side_effect = provider
        def run():
            try: storage.process_cloud_purge(job)
            except storage.JobLeaseLost: errors.append("fenced")
        with patch("app.database.settings",database_dsn=database_dsn(str(self.path))), \
             patch("app.storage.now_iso",return_value=DUE.isoformat()), \
             patch("app.storage.validate_cloud_vault"),patch("app.storage.s3_client",return_value=client):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=20))
                with self.connection() as connection:
                    connection.execute("UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00'")
                    # Equivalent durable transition after exact-version reconciliation:
                    # the replacement observes all targets still present.
                    connection.execute("UPDATE jobs SET status='queued',claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL")
                    rows = ArchiveCatalog(connection).claim_purge_group(lead_job_id=scheduled.job_ids[0],
                        claim_token="replacement",claimed_at=DUE.isoformat(),
                        claim_expires_at=(DUE+timedelta(hours=1)).isoformat(),now=DUE.isoformat(),
                        message="delete",message_key="job.cloud_purge_deleting")
                    self.assertTrue(rows)
            finally:
                release.set()
                worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,["fenced"])
        with self.connection() as connection:
            job = connection.execute("SELECT status,claim_token FROM jobs").fetchone()
            self.assertEqual((job["status"],job["claim_token"]),("cleaning","replacement"))
            self.assertEqual({r["status"] for r in connection.execute("SELECT status FROM cloud_deletion_items").fetchall()},{"pending"})
            self.assertEqual({r["availability"] for r in connection.execute("SELECT availability FROM archive_versions").fetchall()},{"available"})

    def test_failed_stop_deletion_needs_explicit_keep_before_releasing_root(self):
        with self.connection() as connection:
            result = protection_stop.stop_protection(connection,vault_id=1,actor_user_id=1,
                cloud_disposition="delete",confirmation="Photos",reason="stop")
            connection.execute("UPDATE jobs SET status='failed' WHERE group_id=%s",(result["group_id"],))
            protection_stop.reconcile_one(connection,1)
            self.assertFalse(protection_stop.status(connection,1)["root_released"])
            result = protection_stop.stop_protection(connection,vault_id=1,actor_user_id=1,
                cloud_disposition="keep",confirmation="Photos",reason="keep remaining copies")
            self.assertTrue(result["root_released"])
            self.assertEqual(result["cloud_disposition"],"keep")
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions WHERE availability='available'").fetchone()["n"],2)
