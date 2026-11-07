"""Queue fairness, removed-file closure and bulk cancel (#493, #499, #500; ADR-0026).

Real catalog/Jobs on a disposable PostgreSQL database with a simulated clock and
no S3 network. The pure parts (stall detector, deferral registry, error
classification) need no database.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from watchfiles import Change

from app import main, storage
from app.catalog import ArchiveCatalog
from app.config import settings
from app.services import health, notifications, vault_relocation
from app.sessions import create_session, csrf_token_for
from tests.postgres import PostgresConnection, database_dsn, run_alembic


class QueueStallDetectorTests(unittest.TestCase):
    """No database: the detector is a pure clock-based state machine."""

    def test_idle_queue_with_work_becomes_stalled_after_the_window(self):
        progress = storage._QueueProgress()
        start = progress._last_progress
        stalled, idle = progress.evaluate(5, 0, now=start + storage.QUEUE_STALL_SECONDS - 1)
        self.assertFalse(stalled)
        stalled, idle = progress.evaluate(5, 0, now=start + storage.QUEUE_STALL_SECONDS + 1)
        self.assertTrue(stalled)
        self.assertGreaterEqual(idle, storage.QUEUE_STALL_SECONDS)

    def test_running_work_empty_queue_and_claims_all_count_as_progress(self):
        progress = storage._QueueProgress()
        start = progress._last_progress
        late = start + storage.QUEUE_STALL_SECONDS * 2
        self.assertEqual(progress.evaluate(5, 1, now=late), (False, 0.0))
        self.assertEqual(progress.evaluate(5, 0, now=late + 1)[0], False)
        self.assertEqual(progress.evaluate(0, 0, now=late * 3), (False, 0.0))
        # Idle time restarts after an empty queue.
        self.assertFalse(progress.evaluate(5, 0, now=late * 3 + 1)[0])
        progress.note_progress()
        self.assertFalse(progress.evaluate(5, 0)[0])

    def test_health_report_exposes_queue_status(self):
        health.set_queue_status(stalled=True, queued=7, idle_seconds=321.04)
        try:
            queue = health.queue_status()
            self.assertEqual(queue, {"stalled": True, "queued": 7, "seconds_without_progress": 321.0})
        finally:
            health.set_queue_status(stalled=False, queued=0, idle_seconds=0.0)


class DeferralAndClassificationTests(unittest.TestCase):
    def setUp(self):
        storage._deferred_jobs.clear()
        self.addCleanup(storage._deferred_jobs.clear)

    def test_deferred_jobs_are_scoped_per_database_and_expire(self):
        storage._defer_job("db-a", 7, seconds=60)
        storage._defer_job("db-b", 8, seconds=60)
        self.assertEqual(storage._deferred_job_ids("db-a"), [7])
        self.assertEqual(storage._deferred_job_ids("db-b"), [8])
        storage._defer_job("db-a", 9, seconds=-1)
        self.assertEqual(storage._deferred_job_ids("db-a"), [7])

    def test_source_removal_errors_are_recognised_through_the_chain(self):
        self.assertTrue(storage._is_source_removal_error(storage.UploadSourceRemoved("gone")))
        self.assertTrue(storage._is_source_removal_error(FileNotFoundError(2, "gone")))
        try:
            try:
                raise NotADirectoryError(20, "gone")
            except OSError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            self.assertTrue(storage._is_source_removal_error(outer))
        self.assertFalse(storage._is_source_removal_error(PermissionError(13, "denied")))
        self.assertFalse(storage._is_source_removal_error(RuntimeError("other")))


class QueueFixture(unittest.TestCase):
    """Two Vaults on the shared simulated clock (copied from the #423 fixture)."""

    def setUp(self):
        storage._deferred_jobs.clear()
        self.addCleanup(storage._deferred_jobs.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.database = self.base / "catalog"
        migrated = run_alembic(self.database)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.start = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        self.clock = self.start
        self.access = SimpleNamespace(local_operations_allowed=True, volume_alias=None, volume_health="healthy")
        self.roots: dict[int, Path] = {}
        self.vaults: dict[int, dict] = {}
        with self.db() as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash) "
                "VALUES (1, 'owner', 'Owner', 'hash')"
            )
            for vault_id, slug in ((2, "photos"), (3, "docs")):
                root = self.base / slug
                root.mkdir()
                self.roots[vault_id] = root
                connection.execute(
                    """
                    INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix,
                                       root_identity, root_identity_version)
                    VALUES (%s, %s, %s, %s, 'test-bucket', %s, %s, %s)
                    """,
                    (vault_id, slug, slug.title(), str(root), slug,
                     vault_relocation.root_identity(str(root)), vault_relocation.ROOT_IDENTITY_VERSION),
                )
                self.vaults[vault_id] = dict(
                    connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()
                )
        outer = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return outer.clock.astimezone(tz) if tz else outer.clock.replace(tzinfo=None)

        for patcher in (
            patch("app.storage.runtime_status", {}),
            patch("app.services.operation_policies.datetime", Clock),
            patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(self.database))),
            patch("app.storage.settings", SimpleNamespace(operation_concurrency=1, restore_poll_interval=900)),
            patch("app.storage.source_layout.vault_local_access", side_effect=lambda _: self.access),
            patch("app.storage.now_iso", side_effect=lambda: self.clock.isoformat()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def db(self):
        return PostgresConnection(self.database)

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def write(self, vault_id, name, content=b"data"):
        target = self.roots[vault_id] / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        os.utime(target, (1_500_000_000, 1_500_000_000))
        storage.apply_filesystem_changes(self.vaults[vault_id], {(Change.modified, str(target))})
        return target

    def queue(self, vault_id):
        return storage.queue_vault_auto_uploads(self.vaults[vault_id], now=self.clock)

    def jobs(self, vault_id=None):
        with self.db() as connection:
            if vault_id is None:
                return connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return connection.execute(
                "SELECT * FROM jobs WHERE vault_id=%s ORDER BY id", (vault_id,)
            ).fetchall()

    def candidates(self, **kwargs):
        stamp = self.clock.isoformat()
        with self.db() as connection:
            return ArchiveCatalog(connection).list_claimable_jobs(
                now=stamp, restore_due_before=stamp, **kwargs
            )


class ClaimableWindowTests(QueueFixture):
    def test_a_burst_in_one_vault_does_not_hide_another_vault(self):
        for index in range(30):
            self.write(2, f"burst/{index:02d}.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 30)
        self.advance(10)
        self.write(3, "report.txt")
        self.advance(300)
        self.assertEqual(self.queue(3), 1)
        # A global "oldest N" window would contain only Photos Jobs.
        rows = self.candidates(limit=10, per_vault_limit=3)
        self.assertEqual({row["vault_id"] for row in rows}, {2, 3})
        self.assertEqual(sum(1 for row in rows if row["vault_id"] == 2), 3)

    def test_manual_upload_outranks_older_automatic_uploads_in_the_window(self):
        for index in range(5):
            self.write(2, f"auto/{index}.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 5)
        self.advance(60)
        self.write(2, "manual.jpg")
        with self.db() as connection:
            manual, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="manual.jpg", action="upload", requested_by=1,
                requested_at=self.clock.isoformat(), group_id="manual", is_directory=False,
            )
        rows = self.candidates(limit=10, per_vault_limit=2)
        self.assertEqual(rows[0]["id"], manual[0])
        self.assertEqual(len(rows), 2)

    def test_excluded_jobs_are_removed_before_the_window_is_cut(self):
        for index in range(4):
            self.write(2, f"f{index}.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 4)
        ids = [row["id"] for row in self.jobs(2)]
        rows = self.candidates(limit=10, per_vault_limit=2, exclude_job_ids=ids[:2])
        self.assertEqual([row["id"] for row in rows], ids[2:4])
        self.assertEqual(self.candidates(limit=0), [])

    def test_unrunnable_head_of_queue_does_not_block_following_jobs(self):
        self.write(2, "head.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        head = self.jobs(2)[0]["id"]
        self.advance(5)
        self.write(2, "next.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        rows = self.candidates(limit=5, per_vault_limit=1, exclude_job_ids=[head])
        self.assertEqual([row["path"] for row in rows], ["next.jpg"])


class RemovedFileTests(QueueFixture):
    def run_once(self, *, only_removed=True):
        with patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client") as client, \
                patch("app.storage.upload_s3_file") as upload:
            claimed = storage.process_jobs_once()
            if only_removed:
                client.assert_not_called()
                upload.assert_not_called()
        return claimed

    def test_automatic_upload_of_a_removed_file_is_closed_not_failed(self):
        target = self.write(2, "gone.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        target.unlink()
        self.assertEqual(self.run_once(), 1)
        job = self.jobs(2)[0]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["message_key"], "job.upload_file_removed")
        with self.db() as connection:
            failures = connection.execute(
                "SELECT count(*) AS n FROM notifications WHERE event='job_failed'"
            ).fetchone()["n"]
        self.assertEqual(failures, 0)
        # Nothing is left queued: the queue is empty afterwards.
        self.assertEqual(storage.claimable_queue_depth(), 0)

    def test_removed_file_does_not_stop_another_vault_from_running(self):
        gone = self.write(2, "gone.jpg")
        self.write(3, "stays.txt")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        self.assertEqual(self.queue(3), 1)
        gone.unlink()
        real_process_job = storage.process_job
        reached = []

        def process(job):
            if job["vault_id"] == 3:
                # The healthy Vault is dispatched; its upload is not exercised here.
                reached.append(job["id"])
                with self.db() as connection:
                    connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (job["id"],))
                return
            real_process_job(job)

        with patch("app.storage.process_job", side_effect=process):
            self.assertEqual(self.run_once(only_removed=False), 1)
            self.assertEqual(self.run_once(only_removed=False), 1)
        self.assertEqual(len(reached), 1)
        self.assertEqual({row["vault_id"]: row["status"] for row in self.jobs()}[2], "cancelled")

    def test_unreadable_root_is_not_treated_as_a_removed_file(self):
        target = self.write(2, "keep.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        target.unlink()
        self.access = SimpleNamespace(local_operations_allowed=False, volume_alias="x", volume_health="absent")
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.jobs(2)[0]["status"], "queued")

    def test_file_reappearing_with_new_content_is_admitted_again(self):
        target = self.write(2, "back.jpg", b"first")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        target.unlink()
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.jobs(2)[0]["status"], "cancelled")
        storage.apply_filesystem_changes(self.vaults[2], {(Change.deleted, str(target))})
        self.advance(10)
        self.write(2, "back.jpg", b"second version")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        self.assertEqual(self.jobs(2)[-1]["status"], "queued")


class FailureGroupingTests(QueueFixture):
    def fail_jobs(self, count):
        with self.db() as connection:
            for index in range(count):
                connection.execute(
                    "INSERT INTO vault_files(id, vault_id, status, created_at) VALUES (%s, 2, 'active', %s)",
                    (f"00000000-0000-0000-0000-{index:012d}", self.clock.isoformat()),
                )
                job_id = connection.execute(
                    """
                    INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by,
                                     requested_at, updated_at, origin)
                    VALUES (2, %s, %s, 'upload', 'failed', 1, %s, %s, 'automatic') RETURNING id
                    """,
                    (f"00000000-0000-0000-0000-{index:012d}", f"f{index}.jpg",
                     self.clock.isoformat(), self.clock.isoformat()),
                ).fetchone()["id"]
                notifications.enqueue_job_terminal_notification(connection, job_id=job_id)

    def test_burst_of_failures_folds_into_one_group_problem(self):
        total = notifications.GROUP_THRESHOLD + 15
        self.fail_jobs(total)
        with self.db() as connection:
            singles = connection.execute(
                "SELECT count(*) AS n FROM notifications WHERE event='job_failed'"
            ).fetchone()["n"]
            groups = connection.execute(
                "SELECT * FROM notifications WHERE event='job_failed_group'"
            ).fetchall()
        self.assertEqual(singles, notifications.GROUP_THRESHOLD)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["problem_status"], "open")

    def test_group_resolves_when_every_folded_failure_is_moot(self):
        self.fail_jobs(notifications.GROUP_THRESHOLD + 5)
        with self.db() as connection:
            # The files are gone and none of the failures reached the cloud.
            connection.execute(
                """
                INSERT INTO local_copies(vault_file_id, presence, file_type, size, mtime_ns,
                                         last_seen_at, observed_at)
                SELECT vault_file_id, 'missing', 'regular', 1, 1, %s, %s FROM jobs
                """,
                (self.clock.isoformat(), self.clock.isoformat()),
            )
            notifications.reconcile_problem_states(connection)
            states = connection.execute(
                "SELECT event, problem_status FROM notifications"
            ).fetchall()
        self.assertTrue(states)
        self.assertEqual({row["problem_status"] for row in states}, {"resolved"})


class CancelPendingHttpTests(QueueFixture):
    def setUp(self):
        super().setUp()
        configured = replace(settings, database_dsn=database_dsn(str(self.database)), cookie_secure=False)
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        with self.db() as connection:
            token = create_session(connection, user_id=1, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.headers = {"X-CSRF-Token": csrf}
        for name in ("album/a.jpg", "album/b.jpg", "other/c.jpg"):
            self.write(2, name)
        self.write(3, "d.txt")
        self.advance(300)
        self.queue(2)
        self.queue(3)

    def cancel(self, path, vault_id=2):
        return self.client.post(
            "/api/upload/cancel-pending", json={"path": path},
            params={"vault_id": vault_id}, headers=self.headers,
        )

    def statuses(self, vault_id):
        return {row["path"]: row["status"] for row in self.jobs(vault_id)}

    def test_cancelling_a_folder_leaves_other_folders_and_vaults_alone(self):
        response = self.cancel("album")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cancelled_count"], 2)
        self.assertEqual(self.statuses(2), {
            "album/a.jpg": "cancelled", "album/b.jpg": "cancelled", "other/c.jpg": "queued",
        })
        self.assertEqual(set(self.statuses(3).values()), {"queued"})

    def test_cancelling_the_whole_vault_stops_every_unfinished_upload(self):
        response = self.cancel("")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cancelled_count"], 3)
        self.assertEqual(set(self.statuses(2).values()), {"cancelled"})
        self.assertEqual(set(self.statuses(3).values()), {"queued"})
        # Cancelling again finds nothing to cancel.
        self.assertEqual(self.cancel("").json()["cancelled_count"], 0)

    def test_cancelled_automatic_uploads_are_not_requeued_without_a_change(self):
        self.cancel("")
        self.advance(300)
        self.assertEqual(self.queue(2), 0)

    def test_unauthenticated_and_csrf_less_requests_are_refused(self):
        anonymous = TestClient(main.app, client=("127.0.0.1", 50001))
        self.assertIn(anonymous.post("/api/upload/cancel-pending", json={"path": ""}).status_code, {401, 403})
        self.assertEqual(
            self.client.post("/api/upload/cancel-pending", json={"path": ""}, params={"vault_id": 2}).status_code,
            403,
        )


if __name__ == "__main__":
    unittest.main()
