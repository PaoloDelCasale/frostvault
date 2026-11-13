"""Retention of closed operational history (#596, ADR-0035).

Real uploads (``storage.process_job`` with the synthetic ``VersionedProvider``)
on a disposable PostgreSQL 16 database produce the history; the collector runs
with a clock moved past the windows. It deletes only closed, unreferenced
rows, never Current or open work, and never touches the provider.
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import storage
from app.services import history_retention as retention
from app.services.catalog_events import CatalogEventStore
from tests import test_upload_remediation as remediation_tests
from tests import test_upload_worker as worker

A = worker.A
B = b"second content of the report\n" * 64

_HELPERS = ("setUp", "db", "observe", "write", "queue", "claim", "run_job", "due", "abandon",
            "job", "file_row", "rows", "version")
WINDOWS = retention.Windows(jobs=timedelta(days=30), notifications=timedelta(days=30),
                            audit=timedelta(days=90))


def later(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


class HistoryRetentionTests(unittest.TestCase):
    locals().update({name: getattr(worker.UploadWorkerTests, name) for name in _HELPERS})
    fail_original = remediation_tests.UploadRemediationTests.fail_original

    def collect(self, days: int, **kwargs) -> dict[str, int]:
        return retention.collect(storage.db, now=later(days), windows=WINDOWS, **kwargs)

    def count(self, table: str) -> int:
        return int(self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])

    def drain_outbox(self) -> None:
        """Materialized terminal notifications, read by the Owner."""
        with self.db() as connection:
            connection.execute("DELETE FROM job_terminal_notifications")
            connection.execute("UPDATE notifications SET read_at=now() WHERE read_at IS NULL")

    def provider_calls(self) -> tuple[int, int, int]:
        return (self.provider.puts, self.provider.gets, self.provider.deletes)

    def upload_twice(self) -> tuple[int, int]:
        first = self.queue()
        self.assertTrue(self.run_job(first))
        self.write(B)
        second = self.queue()
        self.assertTrue(self.run_job(second))
        self.assertEqual(self.job(second)["status"], "completed", self.job(second)["message"])
        return first, second

    # -- closed chains -------------------------------------------------------

    def test_closed_upload_history_follows_its_jobs_and_current_stays(self) -> None:
        self.upload_twice()
        self.drain_outbox()
        current = self.file_row()["current_version_id"]
        self.assertEqual(self.count("archive_versions"), 1, "the cleanup removed the superseded one")
        self.assertEqual(self.count("upload_attempts"), 2)
        calls = self.provider_calls()

        inside = self.collect(29)
        self.assertEqual(sum(inside.values()), 0, inside)

        counts = self.collect(31)
        self.assertEqual(counts["jobs"], 2, counts)
        self.assertEqual(self.count("jobs"), 0)
        self.assertEqual(self.count("notifications"), 0)
        # The superseded proof goes; the proof of Current stays with it.
        attempts = self.rows("SELECT * FROM upload_attempts")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(str(self.version(current)["upload_attempt_id"]), str(attempts[0]["id"]))
        self.assertEqual(self.count("upload_snapshots"), 1)
        self.assertEqual(self.count("upload_receipts"), 1)
        self.assertEqual(str(self.file_row()["current_version_id"]), str(current))
        self.assertEqual(self.provider_calls(), calls, "retention never calls the provider")
        self.assertEqual(sum(self.collect(400).values()), 0, "Current is never collected")

    def test_job_still_in_the_outbox_or_with_an_unread_notification_stays(self) -> None:
        first, second = self.upload_twice()
        self.drain_outbox()
        with self.db() as connection:
            connection.execute("INSERT INTO job_terminal_notifications(job_id) VALUES (%s)", (first,))
            connection.execute(
                "INSERT INTO notifications(job_id, vault_id, event, title) "
                "VALUES (%s, 2, 'job.completed', 'done')", (second,))
        self.collect(31)
        self.assertEqual({row["id"] for row in self.rows("SELECT id FROM jobs")}, {first, second})
        self.assertEqual(self.count("upload_attempts"), 2,
                         "an attempt follows its Job: it stays while the Job does")
        with self.db() as connection:
            connection.execute("DELETE FROM job_terminal_notifications")
            connection.execute("UPDATE notifications SET read_at=now()")
        self.collect(31)
        self.assertEqual(self.count("jobs"), 0)
        self.assertEqual(self.count("upload_attempts"), 1)

    def test_open_remediation_and_unsettled_send_are_never_collected(self) -> None:
        original = self.fail_original("accepted")
        self.drain_outbox()
        episode = self.rows("SELECT * FROM upload_remediations")
        self.assertEqual(len(episode), 1)
        self.assertIsNone(episode[0]["closed_at"])
        before = {table: self.count(table) for table in (
            "jobs", "upload_attempts", "upload_snapshots", "upload_receipts",
            "upload_remediations", "files")}
        self.collect(400)
        self.assertEqual({table: self.count(table) for table in before}, before)
        self.assertIsNotNone(self.job(original))

    # -- notifications and audit ----------------------------------------------

    def test_notifications_by_state_and_audit_by_age(self) -> None:
        with self.db() as connection:
            def note(event: str, **columns) -> int:
                names = ["event", "title", *columns]
                values = [event, event, *columns.values()]
                return int(connection.execute(
                    f"INSERT INTO notifications({', '.join(names)}) "
                    f"VALUES ({', '.join(['%s'] * len(values))}) RETURNING id",
                    values).fetchone()["id"])
            read = note("read", read_at=datetime.now(timezone.utc))
            unread = note("unread")
            open_problem = note("open", problem_status="open", dedupe_key="p1",
                                read_at=datetime.now(timezone.utc))
            resolved = note("resolved", problem_status="resolved", dedupe_key="p2",
                            resolved_at=datetime.now(timezone.utc))
            email_only = note("email", in_app_enabled=False)
            pending = note("pending", read_at=datetime.now(timezone.utc))
            connection.execute(
                "INSERT INTO notification_deliveries(notification_id, channel, status) "
                "VALUES (%s, 'email', 'pending'), (%s, 'email', 'delivered')",
                (pending, email_only))
            connection.execute("INSERT INTO audit_events(event, actor) VALUES ('a', 'system')")
        counts = self.collect(31)
        self.assertEqual(counts["notifications"], 3, counts)
        kept = {row["id"] for row in self.rows("SELECT id FROM notifications")}
        self.assertEqual(kept, {unread, open_problem, pending})
        self.assertNotIn(read, kept)
        self.assertNotIn(resolved, kept)
        self.assertEqual(self.count("audit_events"), 1, "audit keeps 90 days")
        self.collect(91)
        self.assertEqual(self.count("audit_events"), 0)

    # -- catalog journal, groups, purged files --------------------------------

    def test_catalog_journal_is_pruned_with_an_explicit_retention_gap(self) -> None:
        with self.db() as connection:
            store = CatalogEventStore(connection)
            for _ in range(5):
                store.append_event(vault_id=2, domain="files", scope="report.txt")
            revision = connection.execute(
                "SELECT revision FROM vault_catalog_revisions WHERE vault_id=2").fetchone()["revision"]
        counts = self.collect(31, batch_size=2)
        self.assertGreaterEqual(counts["catalog_events"], 5, counts)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM catalog_events WHERE vault_id=2")[0]["n"], 0)
        with self.db() as connection:
            page = CatalogEventStore(connection).read_events(vault_id=2, after_revision=1)
        self.assertTrue(page["retention_gap"], page)
        self.assertEqual(page["retained_from_revision"], int(revision) + 1)

    def test_finished_groups_and_purged_file_rows(self) -> None:
        with self.db() as connection:
            def group(stop: bool) -> str:
                gid = connection.execute(
                    "INSERT INTO job_groups(vault_id) VALUES (2) RETURNING id").fetchone()["id"]
                connection.execute(
                    "INSERT INTO cloud_purge_requests(group_id, reason, stop_protection) "
                    "VALUES (%s, 'no longer needed', %s)", (gid, stop))
                return str(gid)
            withdrawn = group(False)
            busy = group(False)
            connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, group_id) "
                "VALUES (2, %s, 'recover', 'queued', %s)", (self.file_id, busy))

            def purged(path: str, presence: str) -> str:
                return str(connection.execute(
                    "INSERT INTO files(vault_id, path, path_since, local_presence, local_file_type, "
                    "local_observed_at, cloud_since) VALUES (2, %s, now(), %s, 'regular', now(), "
                    "now()) RETURNING id", (path, presence)).fetchone()["id"])
            gone = purged("old/gone.txt", "missing")
            local = purged("still-here.txt", "present")
        counts = self.collect(31)
        groups = {str(row["id"]) for row in self.rows("SELECT id FROM job_groups")}
        self.assertEqual(groups, {busy})
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cloud_purge_requests")[0]["n"], 1)
        files = {str(row["id"]) for row in self.rows("SELECT id FROM files")}
        self.assertNotIn(gone, files)
        self.assertIn(local, files)
        self.assertIn(str(self.file_id), files, "a File with a Job stays")
        self.assertEqual(counts["files"], 1, counts)
        dirty = self.rows("SELECT path FROM directory_aggregate_dirty WHERE vault_id=2")
        self.assertTrue(dirty, "the collected row invalidates its directory projection")

    # -- races at COMMIT ------------------------------------------------------

    def cancelled_job(self) -> int:
        job_id = self.queue()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (job_id,))
        return job_id

    def test_reference_held_by_an_open_writer_skips_the_row(self) -> None:
        job_id = self.cancelled_job()
        writer = self.db()
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO notifications(job_id, vault_id, event, title) "
                       "VALUES (%s, 2, 'job.cancelled', 'x')", (job_id,))
        done = threading.Event()
        result: dict = {}

        def run() -> None:
            result.update(self.collect(31))
            done.set()
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(done.wait(30), "the collector never waits for a writer's key lock")
        thread.join()
        self.assertEqual(result["jobs"], 0, result)
        writer.commit()
        self.collect(31)
        self.assertIsNotNone(self.job(job_id), "the committed reference keeps the Job")

    def test_reference_committed_between_read_and_lock_is_rechecked(self) -> None:
        job_id = self.cancelled_job()
        original = retention._lock
        raced: list = []

        def lock_after_commit(connection, spec, ids):
            if spec.table == "jobs":
                raced.extend(ids)
                with self.db() as other:
                    other.execute("INSERT INTO notifications(job_id, vault_id, event, title) "
                                  "VALUES (%s, 2, 'job.cancelled', 'x')", (job_id,))
            return original(connection, spec, ids)
        with patch.object(retention, "_lock", lock_after_commit):
            counts = self.collect(31)
        self.assertEqual(raced, [job_id], "the race was exercised")
        self.assertEqual(counts["jobs"], 0, counts)
        self.assertIsNotNone(self.job(job_id))

    def test_remediation_committed_before_lock_keeps_the_attempt_it_names(self) -> None:
        self.upload_twice()
        self.drain_outbox()
        self.collect(31)  # collects the Jobs and the superseded proof
        self.write(A)
        third = self.queue()
        self.assertTrue(self.run_job(third))
        self.drain_outbox()
        superseded = [row["id"] for row in self.rows(
            "SELECT a.id FROM upload_attempts a WHERE NOT EXISTS ("
            "SELECT 1 FROM archive_versions v WHERE v.upload_attempt_id=a.id)")]
        self.assertEqual(len(superseded), 1)
        original = retention._lock
        raced: list = []

        def lock_after_commit(connection, spec, ids):
            if spec.table == "upload_attempts":
                raced.extend(ids)
                with self.db() as other:
                    other.execute(
                        "INSERT INTO upload_remediations(vault_id, file_id, attempt_id, state, "
                        "reason, due_at) VALUES (2, %s, %s, 'blocked', 'owner', 'infinity')",
                        (self.file_id, superseded[0]))
            return original(connection, spec, ids)
        with patch.object(retention, "_lock", lock_after_commit):
            counts = self.collect(31)
        self.assertEqual([str(i) for i in raced], [str(superseded[0])], "the race was exercised")
        self.assertEqual(counts["upload_attempts"], 0, counts)
        episode = self.rows("SELECT attempt_id FROM upload_remediations")
        self.assertEqual([str(row["attempt_id"]) for row in episode], [str(superseded[0])],
                         "the open episode still names its attempt")


if __name__ == "__main__":
    unittest.main()
