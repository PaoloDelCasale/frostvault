"""The Job queue (#794): the vocabulary of Job states, and the queue on PostgreSQL.

The queue tests run :class:`JobQueue` and :class:`QueueRunner` on the test
database with their own connection opener and clock: nothing of the worker
(``app.storage``) is loaded or patched.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import psycopg

from app.services import job_queue
from app.services.job_queue import (
    JOB_LEASE_HELD_STATUSES,
    OPEN_JOB_SQL,
    TERMINAL_JOB_SQL,
    TERMINAL_JOB_STATUSES,
    JobQueue,
    QueueRunner,
    open_job_sql,
    terminal_job_sql,
)
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault

# One Job per status a plain row can hold (the consent and approval states
# need their request rows and are open by the same rule).
SEEDED = {
    "queued": "upload",
    "retrying": "upload",
    "uploading": "upload",
    "verifying": "upload",
    "cleaning": "free-space",
    "restoring": "recover",
    "downloading": "recover",
    "completed": "upload",
    "failed": "upload",
    "cancelled": "upload",
}


class JobStateVocabularyTests(unittest.TestCase):
    def test_the_predicate_is_the_text_of_the_schema_index(self):
        # ``jobs_one_active_per_file_uq`` is declared with this exact text.
        self.assertEqual(OPEN_JOB_SQL, "status NOT IN ('completed', 'failed', 'cancelled')")
        self.assertEqual(TERMINAL_JOB_SQL, "status IN ('completed', 'failed', 'cancelled')")

    def test_an_alias_only_qualifies_the_column(self):
        self.assertEqual(open_job_sql("j"), "j." + OPEN_JOB_SQL)
        self.assertEqual(terminal_job_sql("peer"), "peer." + TERMINAL_JOB_SQL)

    def test_a_held_status_is_never_terminal(self):
        self.assertEqual(TERMINAL_JOB_STATUSES, ("completed", "failed", "cancelled"))
        self.assertFalse(JOB_LEASE_HELD_STATUSES & set(TERMINAL_JOB_STATUSES))


class OpenJobPredicateTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            self.vault = seed_vault(connection)
            for status, action in SEEDED.items():
                seed_job(connection, vault_id=self.vault["id"], path=f"{status}.bin",
                         action=action, status=status)

    def statuses(self, where: str) -> set[str]:
        with self.database.connect() as connection:
            return {row["status"] for row in connection.execute(
                f"SELECT j.status FROM jobs j WHERE {where}")}

    def test_open_and_terminal_split_every_status_like_the_python_set(self):
        terminal = set(TERMINAL_JOB_STATUSES)
        self.assertEqual(self.statuses(open_job_sql("j")), set(SEEDED) - terminal)
        self.assertEqual(self.statuses(terminal_job_sql("j")), terminal)
        self.assertEqual(self.statuses(OPEN_JOB_SQL), set(SEEDED) - terminal)
        self.assertEqual(self.statuses(TERMINAL_JOB_SQL), terminal)

    def test_a_file_holds_one_open_job_and_any_number_of_terminal_ones(self):
        with self.database.connect() as connection:
            for status in TERMINAL_JOB_STATUSES:
                seed_job(connection, vault_id=self.vault["id"], path="completed.bin",
                         status=status)
            seed_job(connection, vault_id=self.vault["id"], path="completed.bin",
                     status="queued")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.database.connect() as connection:
                seed_job(connection, vault_id=self.vault["id"], path="queued.bin",
                         status="queued")


class QueueRunnerTests(FrostVaultTestCase):
    """Take a Job, record its outcome, settle what a dead worker left behind."""

    noon = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)

    def setUp(self) -> None:
        super().setUp()
        self.clock = self.noon
        self.runner = QueueRunner(
            db=self.database.connect, now_iso=lambda: self.clock.isoformat())
        with self.database.connect() as connection:
            self.vault = seed_vault(connection)
            self.job_id = seed_job(
                connection, vault_id=self.vault["id"], path="a.bin", status="queued")

    def row(self) -> dict:
        with self.database.connect() as connection:
            return connection.execute(
                """SELECT j.status, j.message, l.token AS claim_token,
                          l.expires_at AS claim_expires_at
                   FROM jobs j LEFT JOIN job_leases l ON l.job_id=j.id WHERE j.id=%s""",
                (self.job_id,)).fetchone()

    def nominated(self) -> dict:
        return {"id": self.job_id, "action": "upload", "vault_id": self.vault["id"]}

    def take(self) -> dict:
        job = self.nominated()
        self.assertTrue(self.runner.claim(job, restore_due_before=self.clock.isoformat))
        return job

    def start(self) -> dict:
        """A taken Job its worker moved mid-I/O."""
        job = self.take()
        with job_queue.job_claim_context(job):
            self.assertTrue(self.runner.set_job(self.job_id, "uploading"))
        return job

    def test_a_job_is_taken_once_and_carries_its_lease(self):
        job = self.take()
        held = self.row()
        # The claim is the lease; the status moves when the worker starts.
        self.assertEqual(held["status"], "queued")
        self.assertEqual(str(held["claim_token"]), str(job["claim_token"]))
        self.assertEqual(
            held["claim_expires_at"],
            self.noon + timedelta(seconds=job_queue.JOB_CLAIM_LEASE_SECONDS))
        # Nomination is not acquisition: a second worker gets no claim.
        late = self.nominated()
        self.assertFalse(self.runner.claim(late, restore_due_before=self.clock.isoformat))
        self.assertNotIn("claim_token", late)
        self.assertEqual(str(self.row()["claim_token"]), str(job["claim_token"]))

    def test_the_outcome_is_recorded_by_the_claim_holder_and_ends_the_lease(self):
        job = self.take()
        with job_queue.job_claim_context(job):
            self.assertEqual(job_queue.claim_token_for(self.job_id), job["claim_token"])
            self.runner.ensure_job_active(self.job_id, "stopped")
            self.assertTrue(self.runner.set_job(self.job_id, "verifying"))
            self.assertTrue(self.runner.set_job(self.job_id, "completed", "done"))
        self.assertIsNone(job_queue.claim_token_for(self.job_id))
        self.assertEqual(
            self.row(),
            {"status": "completed", "message": "done", "claim_token": None,
             "claim_expires_at": None})

    def test_a_worker_whose_job_was_cancelled_records_nothing(self):
        job = self.take()
        with self.database.connect() as connection:
            cancelled = JobQueue(connection).cancel_open_jobs(
                vault_id=self.vault["id"], job_ids=[self.job_id], message="Stopped",
                message_key=None, now=self.clock.isoformat())
        self.assertEqual(cancelled, [self.job_id])
        with job_queue.job_claim_context(job):
            self.assertFalse(self.runner.set_job(self.job_id, "completed"))
            with self.assertRaises(job_queue.JobLeaseLost):
                self.runner.ensure_job_active(self.job_id, "stopped")
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertIsNone(self.row()["claim_token"])

    def test_the_two_cancellations_are_different_things(self):
        # The signal tells this process; it writes nothing.
        job_queue.signal_job_cancellation([self.job_id])
        self.addCleanup(job_queue.forget_job_cancellation, self.job_id)
        self.assertTrue(job_queue.job_cancelled(self.job_id))
        self.assertEqual(self.row()["status"], "queued")
        with self.assertRaisesRegex(job_queue.OperationCancelled, "stopped"):
            self.runner.ensure_job_active(self.job_id, "stopped")
        job_queue.forget_job_cancellation(self.job_id)
        # The durable cancellation ends the row; it signals no thread.
        with self.database.connect() as connection:
            JobQueue(connection).cancel_open_jobs(
                vault_id=self.vault["id"], job_ids=[self.job_id], message="Stopped",
                message_key=None, now=self.clock.isoformat())
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertFalse(job_queue.job_cancelled(self.job_id))

    def test_an_expired_lease_is_handed_to_the_worker_and_settled_once(self):
        self.start()
        seen = []

        def requeue(connection, job, *, timestamp):
            seen.append((job["id"], job["status"]))
            settled = job_queue.settle_interrupted_job(
                connection, job_id=job["id"], status="queued", message="again",
                timestamp=timestamp, reset_progress=True)
            return "requeued" if settled else "skipped"

        def sweep():
            return self.runner.reconcile_interrupted_jobs(
                reachable=lambda job: True, reconcile_job=requeue,
                reconcile_purge_group=self.fail)

        # A live lease is authoritative: nothing is interrupted yet.
        self.assertEqual(sweep(), {"completed": 0, "requeued": 0, "failed": 0})
        self.assertEqual(seen, [])
        self.clock += timedelta(seconds=job_queue.JOB_CLAIM_LEASE_SECONDS + 1)
        self.assertEqual(sweep(), {"completed": 0, "requeued": 1, "failed": 0})
        self.assertEqual(seen, [(self.job_id, "uploading")])
        self.assertEqual(
            self.row(),
            {"status": "queued", "message": "again", "claim_token": None,
             "claim_expires_at": None})
        self.assertEqual(sweep(), {"completed": 0, "requeued": 0, "failed": 0})

    def test_an_unreachable_job_stays_interrupted(self):
        self.start()
        self.clock += timedelta(seconds=job_queue.JOB_CLAIM_LEASE_SECONDS + 1)
        summary = self.runner.reconcile_interrupted_jobs(
            reachable=lambda job: False, reconcile_job=self.fail,
            reconcile_purge_group=self.fail)
        self.assertEqual(summary, {"completed": 0, "requeued": 0, "failed": 0})
        self.assertEqual(self.row()["status"], "uploading")


if __name__ == "__main__":
    unittest.main()
