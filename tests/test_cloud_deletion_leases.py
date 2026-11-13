"""Destructive-group lease, fencing and partial-failure regressions (#428).

Rebuilt model (#594, ADR-0035): a purge group is claimed all-or-nothing in
``job_leases``; each file's grant names exactly its consented Current. Real
claim and worker on disposable PostgreSQL 16 with the synthetic
``VersionedProvider`` (fixtures from ``tests.test_cloud_purge``).

Former cases now owned by ``tests.test_cloud_purge``:
- restart finalizes recorded deletions without a second provider call →
  ``test_an_unrecorded_delete_is_settled_on_restart``,
  ``test_an_ambiguous_delete_is_settled_by_exact_listing``;
- a failed Stop Protection deletion needs an explicit keep before the root
  is released → ``test_keep_after_a_failed_stop_deletion_releases_the_root``.
"""
from __future__ import annotations

import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import cloud_deletion
from tests import test_cloud_deletion as fixtures


class CloudDeletionLeaseTests(fixtures.CloudDeletionFixture):

    def two_files(self) -> tuple[dict, cloud_deletion.ScheduledDeletion]:
        current = self.cloud_only()
        self.second_cloud_only()
        return current, self.schedule(paths=["report.txt", "other.txt"])

    def raw_claim(self, lead: int, token: str) -> list:
        stamp = storage.now_iso()
        with self.db() as connection:
            return ArchiveCatalog(connection).claim_purge_group(
                lead_job_id=lead, claim_token=token, claimed_at=stamp,
                claim_expires_at=storage._claim_expiry_at(stamp), now=stamp,
                message="Deleting", message_key="job.cloud_purge_deleting")

    def leases(self, group_id: str) -> set[str]:
        return {str(r["token"]) for r in self.rows(
            """SELECT l.token FROM job_leases l JOIN jobs j ON j.id=l.job_id
               WHERE j.group_id=%s""", group_id)}

    def test_two_schedulers_claim_entire_group_once(self) -> None:
        _current, scheduled = self.two_files()
        barrier = threading.Barrier(2)

        def claim(number: int) -> list:
            barrier.wait(timeout=20)
            return self.raw_claim(scheduled.job_ids[number], f"00000000-0000-0000-0000-00000000000{number}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sorted(len(r) for r in results), [0, 2])
        self.assertEqual(set(self.statuses(scheduled.group_id)), {"cleaning"})
        self.assertEqual(len(self.leases(scheduled.group_id)), 1, "one lease token for the whole group")

    def test_cancellation_racing_due_claim_is_all_or_nothing(self) -> None:
        _current, scheduled = self.two_files()
        barrier = threading.Barrier(2)

        def cancel() -> bool:
            with self.db() as connection:
                barrier.wait(timeout=20)
                try:
                    cloud_deletion.cancel_cloud_deletion(
                        connection, vault_id=2, group_id=scheduled.group_id, actor="owner",
                        cancelled_at=storage.now_iso())
                    return True
                except cloud_deletion.CloudDeletionError:
                    return False

        def claim() -> list:
            barrier.wait(timeout=20)
            return self.raw_claim(scheduled.job_ids[0], str(uuid.uuid4()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled, claimed = pool.submit(cancel), pool.submit(claim)
            cancel_won, rows = cancelled.result(timeout=30), claimed.result(timeout=30)
        self.assertEqual(set(self.statuses(scheduled.group_id)),
                         {"cancelled"} if cancel_won else {"cleaning"})
        self.assertEqual(len(rows), 0 if cancel_won else 2)
        self.assertEqual(len(self.leases(scheduled.group_id)), 0 if cancel_won else 1)

    def test_provider_partial_failure_preserves_remaining_copy_and_requires_new_delay(self) -> None:
        current, scheduled = self.two_files()
        delete = self.provider.delete_objects

        def refuse_other(**kwargs):
            kept = [o for o in kwargs["Delete"]["Objects"] if o["Key"].endswith("/other.txt")]
            rest = [o for o in kwargs["Delete"]["Objects"] if o not in kept]
            if rest:
                delete(**{**kwargs, "Delete": {**kwargs["Delete"], "Objects": rest}})
            return {"Errors": [{"Key": o["Key"], "VersionId": o["VersionId"], "Code": "AccessDenied",
                                "Message": "denied"} for o in kept]}
        self.provider.delete_objects = refuse_other
        self.run_purge(scheduled.job_ids[0])
        self.assertEqual(sorted(self.statuses(scheduled.group_id)), ["completed", "failed"])
        self.assert_deleted(current)
        other = self.rows("""SELECT f.current_version_id FROM files f WHERE f.path='other.txt'""")[0]
        self.assertIsNotNone(other["current_version_id"], "the refused copy stays Current")
        self.provider.delete_objects = delete
        again = self.schedule(paths=["other.txt"], requested_at=storage.now_iso())
        self.assertIsNone(self.claim_group(again.job_ids[0]), "a new request waits a new delay")
        self.assertEqual([str(r["archive_version_id"]) for r in self.rows(
            "SELECT archive_version_id FROM jobs WHERE group_id=%s", again.group_id)],
                         [str(other["current_version_id"])])

    def test_a_group_larger_than_one_batch_completes_with_the_lead_settled_last(self) -> None:
        current, scheduled = self.two_files()
        batches = []
        delete = self.provider.delete_objects

        def record(**kwargs):
            batches.append([o["Key"].rsplit("/", 1)[1] for o in kwargs["Delete"]["Objects"]])
            return delete(**kwargs)
        self.provider.delete_objects = record
        lead = scheduled.job_ids[0]
        lead_path = self.rows("SELECT f.path FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.id=%s",
                              lead)[0]["path"]
        with patch("app.storage.CLOUD_DELETE_BATCH_SIZE", 1):
            self.run_purge(lead)  # the scheduler path: the lead's claim is checkpointed
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[-1], [lead_path], "the lead is settled in the last batch")
        self.assertEqual(self.statuses(scheduled.group_id), ["completed", "completed"])
        self.assert_deleted(current)

    def test_reappearance_between_provider_batches_prevents_the_next_deletion(self) -> None:
        _current, scheduled = self.two_files()
        delete = self.provider.delete_objects
        calls = []

        def first_batch(**kwargs):
            [target] = [o["Key"].rsplit("/", 1)[1] for o in kwargs["Delete"]["Objects"]]
            calls.append(target)
            later = "report.txt" if target == "other.txt" else "other.txt"
            (self.source / later).write_bytes(b"new local data")
            return delete(**kwargs)
        self.provider.delete_objects = first_batch
        with patch("app.storage.CLOUD_DELETE_BATCH_SIZE", 1):
            self.run_purge(scheduled.job_ids[0])
        self.assertEqual(len(calls), 1, "the next deletion never runs")
        later = "report.txt" if calls[0] == "other.txt" else "other.txt"
        self.assertEqual((self.source / later).read_bytes(), b"new local data")
        self.assertEqual(sorted(self.statuses(scheduled.group_id)), ["cancelled", "completed"])
        survivor = self.rows("SELECT current_version_id FROM files WHERE path=%s", later)[0]
        self.assertIsNotNone(survivor["current_version_id"])
        self.assertIn((f"vaults/{self.uuid}/{later}",
                       self.rows("SELECT provider_version_id FROM archive_versions WHERE id=%s",
                                 survivor["current_version_id"])[0]["provider_version_id"]),
                      self.provider.objects)

    def test_lost_claim_after_the_delete_never_publishes_the_result(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        job = self.claim_group(scheduled.job_ids[0])
        replacement = str(uuid.uuid4())
        renew = ArchiveCatalog.renew_purge_group_claim
        calls = []

        def taken_over(catalog, **kwargs):
            calls.append(1)
            if len(calls) == 3:  # freeze, send, then the record step
                # The lease expired while the DELETE was answered: another
                # worker took the group over before the result was recorded.
                with self.db() as connection:
                    connection.execute("UPDATE job_leases SET token=%s WHERE job_id=%s",
                                       (replacement, job["id"]))
            return renew(catalog, **kwargs)
        with patch.object(ArchiveCatalog, "renew_purge_group_claim", taken_over), \
                self.assertRaises(storage.JobLeaseLost):
            storage.process_cloud_purge(job)
        self.assertEqual(self.provider.delete_requests, 1)
        self.assertEqual(self.statuses(scheduled.group_id), ["cleaning"], "held, never failed")
        self.assertEqual(self.leases(scheduled.group_id), {replacement})
        # The fenced worker published nothing: the row and its open grant stay
        # for the lease holder, which settles them by exact listing.
        self.assertEqual(self.file_row()["current_version_id"], current["id"])
        self.assertEqual(len(self.rows("SELECT id FROM cleanup_batches")), 1)


if __name__ == "__main__":
    unittest.main()
