"""Queue fairness, removed-file closure and bulk cancel (#493, #499, #500; ADR-0026).

Real catalog/Jobs on a disposable PostgreSQL database with a simulated clock and
no S3 network. The pure parts (stall detector, deferral registry, error
classification) need no database.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
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
from app.services.operation_policies import queue_auto_uploads
from app.sessions import create_session, csrf_token_for
from tests.owner_fixtures import insert_owner, insert_vault
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
            insert_owner(connection)
            for vault_id, slug in ((2, "photos"), (3, "docs")):
                root = self.base / slug
                root.mkdir()
                self.roots[vault_id] = root
                insert_vault(
                    connection, id=vault_id, name=slug.title(), source_root=str(root),
                    bucket="test-bucket",
                    root_identity=vault_relocation.root_identity(str(root)),
                    root_identity_version=vault_relocation.ROOT_IDENTITY_VERSION,
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
            # Per-Vault operation policies are not in the v0.6 baseline yet.
            patch("app.catalog.ArchiveCatalog._upload_verification_mode", return_value="s3_checksum"),
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
        # The scanner and the automatic-upload policy move to the new model
        # with #593/#590: observe the Local Copy through the catalog directly.
        with self.db() as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=vault_id, path=name, file_type="regular", size=len(content),
                mtime_ns=1_500_000_000 * 10**9, observed_at=self.clock.isoformat(),
            )
        return target

    def queue(self, vault_id):
        """Run the real automatic admission (stability window on the clock)."""
        with self.db() as connection:
            return queue_auto_uploads(
                connection, vault_id=vault_id, source_root=str(self.roots[vault_id]),
                now=self.clock,
            )

    def jobs(self, vault_id=None):
        with self.db() as connection:
            if vault_id is None:
                return connection.execute(
                    "SELECT j.*, f.path FROM jobs j JOIN files f ON f.id=j.file_id ORDER BY j.id"
                ).fetchall()
            return connection.execute(
                "SELECT j.*, f.path FROM jobs j JOIN files f ON f.id=j.file_id "
                "WHERE j.vault_id=%s ORDER BY j.id", (vault_id,)
            ).fetchall()

    def add_intent(self, job_id, *, send_started):
        """Open a send attempt of the Job (the provenance machinery's own rows)."""
        with self.db() as connection:
            file_id = connection.execute(
                "SELECT file_id FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()["file_id"]
            digest = b"\x01" * 32
            snapshot = connection.execute(
                """INSERT INTO upload_snapshots(vault_id,file_id,path,path_since,source_root,provider,
                       local_generation,size,mtime_ns,plaintext_sha256,part_size,part_sha256,verification)
                   SELECT vault_id,id,path,path_since,%s,'s3',0,4,1,%s,8388608,ARRAY[%s::bytea],'read_back'
                   FROM files WHERE id=%s RETURNING id""",
                (str(self.roots[2]), digest, digest, file_id),
            ).fetchone()["id"]
            attempt = connection.execute(
                "INSERT INTO upload_attempts(snapshot_id,file_id,job_id,inventory_versions,inventory_markers) "
                "VALUES (%s,%s,%s,'{}','{}') RETURNING id", (snapshot, file_id, job_id),
            ).fetchone()["id"]
            connection.execute("INSERT INTO job_uploads(job_id,verification_mode,snapshot_id) "
                               "VALUES (%s,'read_back',%s) ON CONFLICT (job_id) DO UPDATE "
                               "SET snapshot_id=EXCLUDED.snapshot_id", (job_id, snapshot))
            if send_started:
                connection.execute("UPDATE upload_attempts SET send_started_at=%s WHERE id=%s",
                                   (self.clock.isoformat(), attempt))

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
                vault_id=2, path="manual.jpg", action="upload",
                requested_at=self.clock.isoformat(), group_id=str(uuid.uuid4()), is_directory=False,
            )
        # The window is cut per Vault and per tier: the manual upload is its
        # own tier, so older automatic ones can never push it out of the window.
        rows = self.candidates(limit=10, per_vault_limit=2)
        self.assertEqual(rows[0]["id"], manual[0])
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(1 for row in rows if row["origin"] == "automatic"), 2)

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


class TierWindowTests(QueueFixture):
    def add_owner_operations(self, count, *, action="rename", origin="manual"):
        with self.db() as connection:
            for index in range(count):
                file_id = connection.execute(
                    "INSERT INTO files(vault_id, path, path_since) VALUES (2, %s, %s) RETURNING id",
                    (f"op{index}.jpg", self.clock.isoformat()),
                ).fetchone()["id"]
                connection.execute(
                    """
                    INSERT INTO jobs(vault_id, file_id, action, status,
                                     requested_at, updated_at, origin)
                    VALUES (2, %s, %s, 'queued', %s, %s, %s)
                    """,
                    (file_id, action, self.clock.isoformat(), self.clock.isoformat(), origin),
                )

    def test_a_flood_of_owner_operations_does_not_hide_uploads_of_the_same_vault(self):
        self.write(2, "upload.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        self.advance(60)
        self.add_owner_operations(12)
        rows = self.candidates(limit=50, per_vault_limit=3)
        self.assertEqual([row["action"] for row in rows].count("rename"), 3)
        self.assertEqual([row["path"] for row in rows if row["action"] == "upload"], ["upload.jpg"])
        # Owner operations still come first.
        self.assertEqual(rows[0]["action"], "rename")

    def test_automatic_cleanup_ranks_with_automatic_uploads_not_owner_operations(self):
        # A Job keeps its action and origin: admit the cleanup as automatic.
        self.add_owner_operations(1, action="free-space", origin="automatic")
        self.write(2, "manual.jpg")
        self.advance(300)
        with self.db() as connection:
            manual, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="manual.jpg", action="upload",
                requested_at=self.clock.isoformat(), group_id=str(uuid.uuid4()), is_directory=False,
            )
        rows = self.candidates(limit=10, per_vault_limit=5)
        self.assertEqual(rows[0]["id"], manual[0])

    def test_active_job_counts_per_vault_use_live_leases_only(self):
        self.write(2, "a.jpg")
        self.write(3, "b.jpg")
        self.advance(300)
        self.queue(2)
        self.queue(3)
        live = (self.clock + timedelta(minutes=5)).isoformat()
        stale = (self.clock - timedelta(minutes=5)).isoformat()
        with self.db() as connection:
            for vault_id, expires_at in ((2, live), (3, stale)):
                connection.execute(
                    """
                    INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                    SELECT id, gen_random_uuid(), %s::timestamptz - interval '10 minutes', %s
                    FROM jobs WHERE vault_id=%s
                    """,
                    (expires_at, expires_at, vault_id),
                )
            counts = ArchiveCatalog(connection).active_job_counts(now=self.clock.isoformat())
        self.assertEqual(counts, {2: 1})


class UnrunnableHeadTests(QueueFixture):
    def test_many_unrunnable_jobs_ahead_do_not_hide_a_ready_job_of_the_same_vault(self):
        stuck = storage.MAX_SCAN_ROUNDS * 5  # several windows deep, more than one tick reads
        for index in range(stuck):
            self.write(2, f"stuck/{index:03d}.jpg")
        self.advance(5)
        self.write(2, "ready.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), stuck + 1)
        reached = []

        def state(job):
            return storage.UPLOAD_NOT_READY if job["path"].startswith("stuck/") else storage.UPLOAD_READY

        def process(job):
            reached.append(job["path"])
            with self.db() as connection:
                connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (job["id"],))

        with patch("app.storage.validate_cloud_vault"), patch("app.storage.process_job", side_effect=process), \
                patch("app.storage._automatic_upload_state", side_effect=state), \
                patch("app.storage.observe_failed_job_backlog", return_value=(0, 0.0)), \
                patch("app.storage.cloud_preflight_gate.snapshot", return_value={"status": "ready"}), \
                patch("app.storage.cloud_preflight_gate.dispatch_ready", return_value=True), \
                patch("app.storage.sweep_source_volume_problems", return_value=0):
            self.assertEqual(storage.process_jobs_once(), 1)
            # The next ticks do not re-read the Jobs set aside: nothing else is runnable.
            self.assertEqual(storage.process_jobs_once(), 0)
        self.assertEqual(reached, ["ready.jpg"])

    def test_deferral_backs_off_and_forgets_ready_jobs(self):
        storage._defer_job("db", 1)
        first = storage._deferred_jobs[("db", 1)]
        storage._defer_job("db", 1)
        second = storage._deferred_jobs[("db", 1)]
        self.assertGreater(second[0] - first[0], 0)
        self.assertEqual(second[1], first[1] + 1)
        for _ in range(12):
            storage._defer_job("db", 1)
        remaining = storage._deferred_jobs[("db", 1)][0] - __import__("time").monotonic()
        self.assertLessEqual(remaining, storage.UPLOAD_DEFER_MAX_SECONDS)
        storage._clear_deferral("db", 1)
        self.assertNotIn(("db", 1), storage._deferred_jobs)

    def test_deferred_jobs_are_not_counted_as_runnable_work(self):
        self.write(2, "a.jpg")
        self.write(2, "b.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 2)
        self.assertEqual(storage.claimable_queue_depth(), 2)
        with self.db() as connection:
            scope = storage._database_scope(connection)
        storage._defer_job(scope, self.jobs(2)[0]["id"], seconds=60)
        self.assertEqual(storage.claimable_queue_depth(), 2)
        self.assertEqual(storage.claimable_queue_depth(exclude_deferred=True), 1)


class QueueStallProblemTests(QueueFixture):
    def setUp(self):
        super().setUp()
        for name in ("_queue_problem_open", "_queue_last_stalled_at", "_queue_problem_updated_at"):
            patcher = patch.object(storage, name, None if name == "_queue_problem_open" else 0.0)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(storage, "_queue_progress", storage._QueueProgress())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("app.services.notifications.now_iso", side_effect=lambda: self.clock.isoformat())
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self):
        with self.db() as connection:
            return connection.execute(
                "SELECT problem_status, read_at, message_params FROM notifications WHERE dedupe_key='queue_stalled'"
            ).fetchone()

    def test_a_claim_between_stalls_does_not_reopen_an_unread_alert_each_cycle(self):
        start = storage._queue_progress._last_progress
        stall = storage.QUEUE_STALL_SECONDS + 1
        self.assertTrue(storage.observe_queue_stall(3, now=start + stall))
        first = self.problem()
        self.assertEqual(first["problem_status"], "open")
        # A claim resets the detector, the Owner has not read the alert yet.
        later = start + stall + 5
        storage._queue_progress._last_progress = later - 1  # a claim just now
        self.assertFalse(storage.observe_queue_stall(3, now=later))
        self.assertEqual(self.problem()["problem_status"], "open")
        # Five idle minutes again: still the same open problem, not a new unread one.
        again = later + storage.QUEUE_STALL_SECONDS + 1
        storage._queue_progress._last_progress = later
        self.assertTrue(storage.observe_queue_stall(3, now=again))
        self.assertEqual(self.problem()["problem_status"], "open")
        with self.db() as connection:
            rows = connection.execute("SELECT count(*) AS n FROM notifications WHERE event='queue_stalled'").fetchone()["n"]
        self.assertEqual(rows, 1)

    def test_problem_resolves_only_after_sustained_recovery_and_minutes_are_refreshed(self):
        start = storage._queue_progress._last_progress
        self.assertTrue(storage.observe_queue_stall(4, now=start + storage.QUEUE_STALL_SECONDS + 1))
        before = self.problem()["message_params"]
        later = start + storage.QUEUE_STALL_SECONDS + 1 + storage.QUEUE_STALL_REFRESH_SECONDS + 1
        self.assertTrue(storage.observe_queue_stall(4, now=later))
        self.assertNotEqual(self.problem()["message_params"], before)
        # The queue empties: the alert waits for the recovery period before it resolves.
        self.assertFalse(storage.observe_queue_stall(0, now=later + 10))
        self.assertEqual(self.problem()["problem_status"], "open")
        self.assertFalse(storage.observe_queue_stall(0, now=later + storage.QUEUE_STALL_RECOVERY_SECONDS + 1))
        self.assertEqual(self.problem()["problem_status"], "resolved")


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

    def queue_manual(self, name):
        with self.db() as connection:
            ids, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path=name, action="upload",
                requested_at=self.clock.isoformat(), group_id=str(uuid.uuid4()), is_directory=False,
            )
        return ids[0]

    def test_manual_upload_of_a_removed_file_is_closed_after_proof_of_absence(self):
        # The worker's own error path: the file vanished after the claim, the
        # root is healthy and nothing was sent, so the closure is proven.
        target = self.write(2, "manual-gone.jpg")
        job_id = self.queue_manual("manual-gone.jpg")
        target.unlink()
        self.assertEqual(self.run_once(), 1)
        job = {row["id"]: row for row in self.jobs(2)}[job_id]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["message_key"], "job.upload_file_removed")
        with self.db() as connection:
            failures = connection.execute(
                "SELECT count(*) AS n FROM notifications WHERE event='job_failed'"
            ).fetchone()["n"]
            presence = connection.execute(
                "SELECT f.local_presence FROM files f JOIN jobs j ON j.file_id=f.id "
                "WHERE j.id=%s", (job_id,),
            ).fetchone()["local_presence"]
        self.assertEqual(failures, 0)
        self.assertEqual(presence, "missing")

    def test_manual_upload_is_not_closed_when_the_root_is_lost_after_enoent(self):
        # The ENOENT is real, but the root fails the second check (an unmount
        # racing the stat): the absence of the file is not proven.
        target = self.write(2, "manual-keep.jpg")
        job_id = self.queue_manual("manual-keep.jpg")
        target.unlink()
        with patch("app.storage._enrolled_root_still_healthy", return_value=False):
            self.run_once()
        job = {row["id"]: row for row in self.jobs(2)}[job_id]
        self.assertNotEqual(job["status"], "cancelled")
        self.assertNotEqual(job["message_key"], "job.upload_file_removed")

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


    def job_context(self):
        job = dict(self.jobs(2)[0])
        job["source_root"] = str(self.roots[2])
        return job

    def test_closing_records_the_absence_so_identical_bytes_are_admitted_again(self):
        target = self.write(2, "same.jpg", b"identical")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        target.unlink()
        self.assertEqual(self.run_once(), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT local_presence FROM files").fetchone()["local_presence"], "missing")
        # The same bytes (same size and mtime) come back.
        self.advance(10)
        self.write(2, "same.jpg", b"identical")
        self.assertEqual(self.queue(2), 0)  # a new stability window, not instantly
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        self.assertEqual([row["status"] for row in self.jobs(2)], ["cancelled", "queued"])

    def test_confirmed_removal_requires_a_healthy_root_and_no_started_send(self):
        target = self.write(2, "gone.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        job = self.job_context()
        self.assertFalse(storage._upload_source_confirmed_removed(job))  # the file is still there
        target.unlink()
        self.assertTrue(storage._upload_source_confirmed_removed(job))
        # A started send may already be in S3: never closed from the database alone.
        self.add_intent(job["id"], send_started=True)
        self.assertFalse(storage._upload_source_confirmed_removed(job))

    def test_an_absent_or_replaced_root_is_never_a_removed_file(self):
        self.write(2, "keep.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        job = self.job_context()
        original = self.base / "photos-moved"
        self.roots[2].rename(original)
        try:
            # Real filesystem state: the enrolled folder is gone (unmounted).
            self.assertEqual(storage._automatic_upload_state(job), storage.UPLOAD_NOT_READY)
            self.assertFalse(storage._upload_source_confirmed_removed(job))
            self.assertEqual(self.run_once(), 0)
            self.assertEqual(self.jobs(2)[0]["status"], "queued")
            # Replaced by another, empty folder: identity no longer matches.
            self.roots[2].mkdir()
            self.assertEqual(storage._automatic_upload_state(job), storage.UPLOAD_NOT_READY)
            self.assertFalse(storage._upload_source_confirmed_removed(job))
            self.assertEqual(self.jobs(2)[0]["status"], "queued")
        finally:
            if self.roots[2].exists():
                self.roots[2].rmdir()
            original.rename(self.roots[2])

    def test_a_root_lost_right_after_enoent_is_not_a_removed_file(self):
        target = self.write(2, "race.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        job = self.job_context()
        target.unlink()
        with patch("app.storage._enrolled_root_still_healthy", return_value=False):
            self.assertEqual(storage._automatic_upload_state(job), storage.UPLOAD_NOT_READY)
            self.assertFalse(storage._upload_source_confirmed_removed(job))
        self.assertEqual(storage._automatic_upload_state(job), storage.UPLOAD_SOURCE_REMOVED)

    def add_episode(self, job, *, current):
        """An open Upload Remediation episode of the Job's file, linked to it."""
        with self.db() as connection:
            episode = connection.execute(
                "INSERT INTO upload_remediations(vault_id, file_id, state, due_at, current_job_id) "
                "VALUES (2, %s, 'queued', %s, %s) RETURNING id",
                (job["file_id"], self.clock.isoformat(), job["id"] if current else None),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO upload_remediation_jobs(job_id, remediation_id) VALUES (%s, %s)",
                (job["id"], episode),
            )
        return episode

    def test_rearmed_remediation_closes_as_file_removed_when_the_file_is_gone(self):
        from app.services.upload_remediation import FILE_REMOVED_SQL
        target = self.write(2, "episode.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        job = self.jobs(2)[0]
        # After a proven absence the episode's Job is an ordinary upload again
        # (ADR-0025): nothing is left in the cloud, so the removal closes it.
        episode = self.add_episode(job, current=True)
        target.unlink()
        self.assertEqual(self.run_once(), 1)
        with self.db() as connection:
            row = connection.execute(
                "SELECT state, reason FROM upload_remediations WHERE id=%s", (episode,)
            ).fetchone()
            closed = connection.execute(
                f"SELECT original.id FROM jobs original WHERE original.id=%s AND {FILE_REMOVED_SQL}",
                (job["id"],),
            ).fetchone()
        self.assertEqual(row, {"state": "cancelled", "reason": "file_removed"})
        self.assertEqual(closed, {"id": job["id"]})
        self.assertEqual(self.jobs(2)[0]["status"], "cancelled")

    def test_failed_notification_of_a_file_removed_episode_resolves(self):
        self.write(2, "note.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        job = self.jobs(2)[0]
        episode = self.add_episode(job, current=False)
        with self.db() as connection:
            # The failure is notified while its episode is still open...
            connection.execute("UPDATE jobs SET status='failed', finished_at=%s WHERE id=%s",
                               (self.clock.isoformat(), job["id"]))
            notifications.enqueue_job_terminal_notification(connection, job_id=job["id"])
            before = connection.execute(
                "SELECT problem_status FROM notifications WHERE event='job_failed'"
            ).fetchone()["problem_status"]
            # ...and the episode later ends because the file is gone.
            connection.execute(
                "UPDATE upload_remediations SET state='cancelled', reason='file_removed', "
                "closed_at=%s WHERE id=%s", (self.clock.isoformat(), episode),
            )
            notifications.reconcile_problem_states(connection)
            after = connection.execute(
                "SELECT problem_status FROM notifications WHERE event='job_failed'"
            ).fetchone()["problem_status"]
        self.assertEqual((before, after), ("open", "resolved"))


class FailureGroupingTests(QueueFixture):
    def setUp(self):
        super().setUp()
        patcher = patch("app.services.notifications.now_iso", side_effect=lambda: self.clock.isoformat())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.failed = 0

    def fail_jobs(self, count):
        with self.db() as connection:
            for _ in range(count):
                index, self.failed = self.failed, self.failed + 1
                file_id = connection.execute(
                    "INSERT INTO files(vault_id, path, path_since) VALUES (2, %s, %s) RETURNING id",
                    (f"f{index}.jpg", self.clock.isoformat()),
                ).fetchone()["id"]
                job_id = connection.execute(
                    """
                    INSERT INTO jobs(vault_id, file_id, action, status, requested_at,
                                     updated_at, finished_at, origin)
                    VALUES (2, %s, 'upload', 'failed', %s, %s, %s, 'automatic') RETURNING id
                    """,
                    (file_id, self.clock.isoformat(), self.clock.isoformat(),
                     self.clock.isoformat()),
                ).fetchone()["id"]
                notifications.enqueue_job_terminal_notification(connection, job_id=job_id)

    def rows(self, event):
        with self.db() as connection:
            return connection.execute(
                "SELECT * FROM notifications WHERE event=%s ORDER BY id", (event,)
            ).fetchall()

    def group_count(self, row):
        from app.i18n import parse_message_params
        return int(parse_message_params(row["message_params"])["count"])

    def test_burst_of_failures_folds_into_one_group_problem(self):
        total = notifications.GROUP_THRESHOLD + 15
        self.fail_jobs(total)
        singles, groups = self.rows("job_failed"), self.rows("job_failed_group")
        self.assertEqual(len(singles), notifications.GROUP_THRESHOLD)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["problem_status"], "open")
        self.assertEqual(len(singles) + self.group_count(groups[0]), total)

    def test_group_stops_absorbing_after_the_window_and_a_new_failure_has_its_own_row(self):
        self.fail_jobs(notifications.GROUP_THRESHOLD + 5)
        self.advance(notifications.GROUP_WINDOW_SECONDS + 60)
        self.fail_jobs(1)
        self.assertEqual(len(self.rows("job_failed")), notifications.GROUP_THRESHOLD + 1)
        groups = self.rows("job_failed_group")
        self.assertEqual(len(groups), 1)
        self.assertEqual(self.group_count(groups[0]), 5)  # the late failure is not in it
        self.assertNotEqual(groups[0]["dedupe_key"], f"job_failed_group:2")  # retired

    def test_a_new_burst_after_the_window_starts_a_new_group(self):
        self.fail_jobs(notifications.GROUP_THRESHOLD + 5)
        self.advance(notifications.GROUP_WINDOW_SECONDS + 60)
        self.fail_jobs(notifications.GROUP_THRESHOLD + 3)
        groups = self.rows("job_failed_group")
        self.assertEqual(len(groups), 2)
        current = [row for row in groups if row["dedupe_key"] == "job_failed_group:2"]
        self.assertEqual(len(current), 1)
        self.assertEqual(self.group_count(current[0]), 3)

    def test_group_keeps_absorbing_while_the_burst_rate_holds_past_one_window(self):
        self.fail_jobs(notifications.GROUP_THRESHOLD + 2)
        for _ in range(2):
            self.advance(notifications.GROUP_WINDOW_SECONDS * 2 // 3)
            self.fail_jobs(notifications.GROUP_THRESHOLD + 1)
        groups = self.rows("job_failed_group")
        self.assertEqual(len(groups), 1)
        self.assertEqual(self.group_count(groups[0]), 2 + 2 * (notifications.GROUP_THRESHOLD + 1))
        self.assertEqual(len(self.rows("job_failed")), notifications.GROUP_THRESHOLD)

    def test_a_slow_trickle_after_the_burst_is_not_absorbed(self):
        self.fail_jobs(notifications.GROUP_THRESHOLD + 5)
        self.advance(notifications.GROUP_WINDOW_SECONDS * 2 // 3)
        self.fail_jobs(1)  # still inside the burst's window: folded
        self.advance(notifications.GROUP_WINDOW_SECONDS * 2 // 3)
        self.fail_jobs(1)  # the burst is over: a real failure of its own
        groups = self.rows("job_failed_group")
        self.assertEqual(len(groups), 1)
        self.assertEqual(self.group_count(groups[0]), 6)
        self.assertNotEqual(groups[0]["dedupe_key"], "job_failed_group:2")  # retired
        singles = self.rows("job_failed")
        self.assertEqual(len(singles), notifications.GROUP_THRESHOLD + 1)
        self.assertEqual(singles[-1]["problem_status"], "open")

    def test_group_resolves_when_every_folded_failure_is_moot(self):
        self.fail_jobs(notifications.GROUP_THRESHOLD + 5)
        with self.db() as connection:
            # The files are gone and none of the failures reached the cloud.
            connection.execute(
                "UPDATE files SET local_presence='missing', local_file_type='regular', "
                "local_observed_at=%s", (self.clock.isoformat(),),
            )
            notifications.reconcile_problem_states(connection)
            states = connection.execute(
                "SELECT event, problem_status FROM notifications"
            ).fetchall()
        self.assertTrue(states)
        self.assertEqual({row["problem_status"] for row in states}, {"resolved"})

    def test_a_retired_group_resolves_on_its_own_failures_not_later_ones(self):
        count = notifications.GROUP_THRESHOLD + 5
        self.fail_jobs(count)
        self.advance(notifications.GROUP_WINDOW_SECONDS + 60)
        self.fail_jobs(1)  # gets its own row and stays a real failure
        with self.db() as connection:
            connection.execute(
                "UPDATE files SET local_presence='missing', local_file_type='regular', "
                "local_observed_at=%s WHERE path <> %s", (self.clock.isoformat(), f"f{count}.jpg"),
            )
            notifications.reconcile_problem_states(connection)
        self.assertEqual(self.rows("job_failed_group")[0]["problem_status"], "resolved")
        self.assertEqual(self.rows("job_failed")[-1]["problem_status"], "open")


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
            token = create_session(connection, auth_method="oidc")
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

    def queue_manual(self, name):
        group_id = str(uuid.uuid4())
        with self.db() as connection:
            job_ids, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path=name, action="upload",
                requested_at=self.clock.isoformat(), group_id=group_id, is_directory=False,
            )
        return job_ids[0], group_id

    def test_owner_cancelled_manual_uploads_are_not_requeued_automatically(self):
        # Issue #618: cancelling a manual upload must not hand the same files
        # to automatic protection; they come back only when their content changes.
        self.write(2, "manual/a.jpg")
        self.write(2, "single/b.jpg")
        first, _ = self.queue_manual("manual/a.jpg")
        _, group = self.queue_manual("single/b.jpg")
        self.assertEqual(self.cancel("manual").json()["cancelled_count"], 1)
        response = self.client.post("/api/upload/cancel", json={"group_id": group},
                                    params={"vault_id": 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({row["path"]: (row["origin"], row["status"]) for row in self.jobs(2)
                          if row["path"] in {"manual/a.jpg", "single/b.jpg"}},
                         {"manual/a.jpg": ("manual", "cancelled"),
                          "single/b.jpg": ("manual", "cancelled")})
        self.advance(300)
        self.assertEqual(self.queue(2), 0)
        self.advance(10)
        self.write(2, "manual/a.jpg", b"changed content")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        requeued = [row for row in self.jobs(2) if row["status"] == "queued"
                    and row["path"] == "manual/a.jpg"]
        self.assertEqual([row["origin"] for row in requeued], ["automatic"])
        self.assertNotEqual(requeued[0]["id"], first)

    def test_manual_uploads_closed_without_an_owner_cancel_do_not_hold_automatic_admission(self):
        self.write(2, "manual/failed.jpg")
        self.write(2, "manual/removed.jpg")
        failed, _ = self.queue_manual("manual/failed.jpg")
        removed, _ = self.queue_manual("manual/removed.jpg")
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='failed' WHERE id=%s", (failed,))
            connection.execute(
                "UPDATE jobs SET status='cancelled', message_key='job.upload_file_removed' "
                "WHERE id=%s", (removed,))
        self.advance(300)
        self.assertEqual(self.queue(2), 2)

    def test_folder_scope_matches_the_folder_and_its_children_only(self):
        self.write(2, "foo/x.jpg")
        self.write(2, "foobar/y.jpg")
        self.write(2, "foo.jpg")
        self.advance(300)
        self.assertEqual(self.queue(2), 3)
        response = self.cancel("foo")
        self.assertEqual(response.json()["cancelled_count"], 1)
        statuses = self.statuses(2)
        self.assertEqual(statuses["foo/x.jpg"], "cancelled")
        self.assertEqual(statuses["foobar/y.jpg"], "queued")
        self.assertEqual(statuses["foo.jpg"], "queued")

    def test_only_uploads_that_never_started_are_cancelled(self):
        ids = {row["path"]: row["id"] for row in self.jobs(2)}
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='uploading' WHERE id=%s", (ids["album/a.jpg"],))
            connection.execute("UPDATE job_uploads SET reconcile_only=TRUE WHERE job_id=%s",
                               (ids["album/b.jpg"],))
        response = self.cancel("")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cancelled_count"], 1)
        self.assertEqual(self.statuses(2), {
            "album/a.jpg": "uploading", "album/b.jpg": "queued", "other/c.jpg": "cancelled",
        })

    def test_jobs_with_a_bound_or_started_send_and_remediation_episodes_are_left_alone(self):
        ids = {row["path"]: row["id"] for row in self.jobs(2)}
        self.add_intent(ids["album/a.jpg"], send_started=True)
        self.add_intent(ids["album/b.jpg"], send_started=False)
        with self.db() as connection:
            job = connection.execute("SELECT id, file_id FROM jobs WHERE id=%s",
                                     (ids["other/c.jpg"],)).fetchone()
            episode = connection.execute(
                "INSERT INTO upload_remediations(vault_id,file_id,state,due_at,current_job_id) "
                "VALUES (2,%s,'queued',%s,%s) RETURNING id",
                (job["file_id"], self.clock.isoformat(), job["id"]),
            ).fetchone()["id"]
            connection.execute("INSERT INTO upload_remediation_jobs(job_id,remediation_id) VALUES (%s,%s)",
                               (job["id"], episode))
        response = self.cancel("")
        self.assertEqual(response.json()["cancelled_count"], 0)
        self.assertEqual(set(self.statuses(2).values()), {"queued"})
        with self.db() as connection:
            state = connection.execute("SELECT state FROM upload_remediations WHERE id=%s",
                                       (episode,)).fetchone()["state"]
        self.assertEqual(state, "queued")

    def test_unauthenticated_and_csrf_less_requests_are_refused(self):
        anonymous = TestClient(main.app, client=("127.0.0.1", 50001))
        self.assertIn(anonymous.post("/api/upload/cancel-pending", json={"path": ""}).status_code, {401, 403})
        self.assertEqual(
            self.client.post("/api/upload/cancel-pending", json={"path": ""}, params={"vault_id": 2}).status_code,
            403,
        )


if __name__ == "__main__":
    unittest.main()
