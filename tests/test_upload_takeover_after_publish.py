"""#628: a Job taken over after its version was published completes.

A worker stops after ``_publish`` (the version is verified and Current) and
before the final listing that proves the keeper alone at its key (#622,
ADR-0038). After the lease expires another claim resumes the same Job: it
skips reconciliation, send, proof and verification, so nothing on its way has
opened an S3 client yet. The postcondition must still list with a real client
and complete, and a file an earlier release blocked there with a false
``inventory_changed`` closes through "Retry failed uploads" alone.

Real admission, claim and ``storage.process_job`` on disposable PostgreSQL 16
(TEST_POSTGRES_URL is mandatory) with the synthetic ``VersionedProvider``.
"""
import unittest
from unittest.mock import patch

from app import storage
from app.services import notifications, snapshot_cleanup
from app.services import upload_provenance as provenance
from app.services.upload_execution import UploadExecution
from tests import test_upload_provenance_worker as fixtures
from tests.postgres import PostgresConnection


class _WorkerStopped(BaseException):
    """The process stops between two steps: nothing of the run handles it."""


class UploadTakeoverAfterPublishTests(unittest.TestCase):
    setUp = fixtures.UploadProvenanceWorkerTests.setUp
    queue = fixtures.UploadProvenanceWorkerTests.queue
    run_job = fixtures.UploadProvenanceWorkerTests.run_job
    row = fixtures.UploadProvenanceWorkerTests.row
    assert_keeper = fixtures.UploadProvenanceWorkerTests.assert_keeper
    http_client = fixtures.UploadProvenanceWorkerTests.http_client

    def db(self):
        return PostgresConnection(self.db_path)

    def listings(self) -> list[str]:
        seen: list[str] = []
        self.provider.hook = lambda stage, **kwargs: seen.append(stage) if stage == "list" else None
        return seen

    def stopped_after_publish(self) -> int:
        """Run a new file's upload until its version is Current, then stop the worker."""
        original = UploadExecution._publish

        def publish_then_stop(execution, run):
            original(execution, run)
            raise _WorkerStopped()

        job = self.queue()
        with patch.object(UploadExecution, "_publish", publish_then_stop), \
                self.assertRaises(_WorkerStopped):
            self.run_job(job)
        with self.db() as connection:
            row = connection.execute(
                """SELECT f.current_version_id, av.integrity FROM files f
                   JOIN archive_versions av ON av.id = f.current_version_id WHERE f.id=%s""",
                (self.file_id,)).fetchone()
            self.assertEqual(row["integrity"], "verified", "the version is already Current")
            # The lease expires: the Job is taken over as after a restart.
            connection.execute(
                "UPDATE job_leases SET claimed_at='1999-12-31T00:00:00+00:00', "
                "expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=%s", (job,))
        storage.reconcile_interrupted_jobs()
        return job

    def test_a_job_taken_over_after_publication_completes(self):
        job = self.stopped_after_publish()
        self.assertEqual(self.provider.puts, 1)
        lists = self.listings()
        self.run_job(job)
        self.assert_keeper(job)
        # No second send; one listing proves the keeper alone (ADR-0038).
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertEqual(lists, ["list"])
        with self.db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT reconciliation_block FROM files WHERE id=%s",
                (self.file_id,)).fetchone()["reconciliation_block"])
            self.assertIsNone(connection.execute("SELECT 1 FROM upload_remediations").fetchone())

    def test_retry_failed_uploads_closes_a_file_blocked_there_by_an_earlier_release(self):
        job = self.stopped_after_publish()
        # The resumed Job ends as before #628: a false inventory_changed.
        with patch.object(snapshot_cleanup, "_require_sole",
                          side_effect=provenance.UploadBlocked("inventory_changed")):
            self.run_job(job)
        failed = self.row(job)
        self.assertEqual(failed["status"], "failed", failed["message"])
        with self.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT reconciliation_block FROM files WHERE id=%s",
                (self.file_id,)).fetchone()["reconciliation_block"], "inventory_changed")
            episode = connection.execute("SELECT state, reason FROM upload_remediations").fetchone()
            self.assertEqual(episode["reason"], "inventory_changed")
            notifications.reconcile_problem_states(connection)
            opened = connection.execute(
                "SELECT event FROM notifications WHERE problem_status='open' ORDER BY event").fetchall()
            self.assertEqual({n["event"] for n in opened}, {"job_failed", "upload_reconciliation"},
                             "a blocked file keeps its Problems open")

        client = self.http_client()
        response = client.post("/api/upload/retry-failed?vault_id=2")
        self.assertEqual((response.status_code, response.json()), (202, {"item_count": 1}))
        with self.db() as connection:
            retried = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
        lists = self.listings()
        self.run_job(retried)
        self.assert_keeper(retried)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertEqual(lists, ["list"])
        with self.db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT reconciliation_block FROM files WHERE id=%s",
                (self.file_id,)).fetchone()["reconciliation_block"])
            episode = connection.execute(
                "SELECT state, resolved_by_job_id, closed_at FROM upload_remediations").fetchone()
            self.assertEqual((episode["state"], episode["resolved_by_job_id"]), ("resolved", retried))
            self.assertIsNotNone(episode["closed_at"])
            notifications.reconcile_problem_states(connection)
            self.assertEqual(connection.execute(
                "SELECT event, problem_status FROM notifications WHERE problem_status='open'").fetchall(),
                [], "no Problem is left open")
        self.assertEqual(client.post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 0})


if __name__ == "__main__":
    unittest.main()
