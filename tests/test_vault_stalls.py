"""Visible stalls (#759): queued scans, the Stalled Vault problem; DB-free."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app import storage
from app.services import runtime_status as runtime
from app.services.runtime_status import VaultLock, scan_lock_for_vault, snapshot_runtime_status_for_stats

VAULT_ID = 7590


def settle(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class VaultLockTests(unittest.TestCase):
    def test_the_lock_knows_who_holds_it_and_since_when(self):
        lock = VaultLock()
        self.assertIsNone(lock.holder())
        self.assertTrue(lock.acquire(blocking=False, operation="auto_admission"))
        holder = lock.holder()
        self.assertEqual(holder["operation"], "auto_admission")
        self.assertFalse(lock.acquire(blocking=False, operation="full_scan"))
        # A named step keeps the time the Vault was taken.
        lock.relabel("identify_local_copies")
        self.assertEqual(lock.holder()["operation"], "identify_local_copies")
        self.assertEqual(lock.holder()["since"], holder["since"])
        lock.release()
        self.assertIsNone(lock.holder())
        self.assertFalse(lock.locked())
        with lock.holding("free_space"):
            self.assertEqual(lock.holder()["operation"], "free_space")
        with lock:
            self.assertEqual(lock.holder()["operation"], VaultLock.DEFAULT_OPERATION)
        self.assertIsNone(lock.holder())

    def test_unknown_operations_have_a_generic_name(self):
        self.assertEqual(runtime.vault_operation_key("auto_admission"), "vault.operation.auto_admission")
        self.assertEqual(runtime.vault_operation_key("something"), "vault.operation.other")


class QueuedScanTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(runtime.runtime_status.pop, VAULT_ID, None)
        self.addCleanup(runtime.scan_locks.pop, VAULT_ID, None)
        self.addCleanup(storage._queued_scans.pop, VAULT_ID, None)
        patcher = patch.object(storage, "QUEUED_SCAN_POLL_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_scan_requested_while_admission_holds_the_vault_starts_when_it_is_released(self):
        lock = scan_lock_for_vault(VAULT_ID)
        self.assertTrue(lock.acquire(blocking=False, operation="auto_admission"))
        calls: list[str | None] = []

        def scan(vault, *, _on_start=None):
            # The real scan would find the lock free: the queue waited for it.
            calls.append(None if lock.locked() else "free")
            if _on_start:
                _on_start()
            return {"local": 1}

        with patch.object(storage, "scan_vault", side_effect=scan):
            future = storage.queue_scan({"id": VAULT_ID})
            # A second request before the scan starts shares it.
            self.assertIs(storage.queue_scan({"id": VAULT_ID}), future)
            time.sleep(0.1)
            self.assertEqual(calls, [])
            status = snapshot_runtime_status_for_stats(VAULT_ID)
            self.assertTrue(status["scan_queued"])
            self.assertEqual(status["scan_waiting_for"], "auto_admission")
            self.assertEqual(status["scan_waiting_since"], lock.holder()["since"])
            lock.release()
            self.assertEqual(future.result(timeout=5), {"local": 1})
        self.assertEqual(calls, ["free"])
        status = snapshot_runtime_status_for_stats(VAULT_ID)
        self.assertFalse(status["scan_queued"])
        self.assertIsNone(status["scan_waiting_for"])
        self.assertNotIn(VAULT_ID, storage._queued_scans)

    def test_a_request_during_a_running_scan_waits_for_a_scan_of_its_own(self):
        runtime.runtime_status[VAULT_ID] = {"scanning": True, "scan_id": "2026-11-17T17:30:00+00:00"}
        started = threading.Event()

        def scan(vault, *, _on_start=None):
            if _on_start:
                _on_start()
            started.set()
            return {"local": 1}

        with patch.object(storage, "scan_vault", side_effect=scan):
            future = storage.queue_scan({"id": VAULT_ID})
            time.sleep(0.05)
            self.assertFalse(started.is_set())
            # The verifying scan holds no lock (#689): the queue waits for it.
            status = snapshot_runtime_status_for_stats(VAULT_ID)
            self.assertEqual(status["scan_waiting_for"], "full_scan")
            self.assertEqual(status["scan_waiting_since"], "2026-11-17T17:30:00+00:00")
            runtime.runtime_status[VAULT_ID]["scanning"] = False
            self.assertEqual(future.result(timeout=5), {"local": 1})

    def test_a_request_full_scan_is_queued(self):
        with patch.object(storage, "queue_scan") as queue:
            storage.request_full_scan({"id": VAULT_ID})
        queue.assert_called_once_with({"id": VAULT_ID})

    def test_a_scheduled_scan_that_finds_the_vault_busy_is_queued(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        try:
            with patch.object(storage, "queue_scan") as queue:
                storage._scan_or_queue({"id": VAULT_ID})
            queue.assert_called_once()
        finally:
            lock.release()
        # A scan already running is the scan that was due.
        runtime.runtime_status[VAULT_ID] = {"scanning": True}
        with patch.object(storage, "scan_vault", return_value={"already_running": 1}), \
                patch.object(storage, "queue_scan") as queue:
            storage._scan_or_queue({"id": VAULT_ID})
        queue.assert_not_called()

    def test_a_queued_scan_stops_at_shutdown(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        self.addCleanup(lock.release)
        requested = MagicMock(requested=False)
        with patch.object(storage, "shutdown_state", requested), \
                patch.object(storage, "scan_vault") as scan:
            future = storage.queue_scan({"id": VAULT_ID})
            time.sleep(0.05)
            requested.requested = True
            self.assertEqual(future.result(timeout=5), {"stopped": 1})
        scan.assert_not_called()


    def test_a_scan_that_takes_the_vault_first_takes_the_queued_request(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        bodies: list[int] = []

        def body(vault, *, _on_start=None):
            held = scan_lock_for_vault(vault["id"])
            if not held.acquire(blocking=False, operation="full_scan"):
                return {"already_running": 1}
            try:
                if _on_start:
                    _on_start()
                bodies.append(1)
                return {"local": 2}
            finally:
                held.release()

        # The waiting thread finds the Vault busy, then holds until the direct
        # scan is done: no timing decides who gets there first.
        polled, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        real_busy = storage.vault_busy

        def busy(vault_id):
            result = real_busy(vault_id)
            if result and not polled.is_set():
                polled.set()
                resume.wait(5)
            return result

        before = set(storage._watcher_scan_futures)
        with patch.object(storage, "vault_busy", side_effect=busy), \
                patch.object(storage, "_scan_vault", side_effect=body):
            future = storage.queue_scan({"id": VAULT_ID})
            (task,) = set(storage._watcher_scan_futures) - before
            self.assertTrue(polled.wait(5))
            lock.release()
            # A direct scan (scheduled, watcher) gets there first.
            self.assertEqual(storage.scan_vault({"id": VAULT_ID}), {"local": 2})
            self.assertEqual(future.result(timeout=5), {"local": 2})
            self.assertFalse(snapshot_runtime_status_for_stats(VAULT_ID)["scan_queued"])
            resume.set()
            task.result(timeout=5)  # The waiting thread wakes up and finds it done.
        self.assertEqual(bodies, [1], "no second scan for the same request")

    def test_a_watcher_registered_behind_a_queued_scan_shares_it(self):
        # The queued scan waits on the Vault's only paired-scan thread (#763):
        # a watcher scan submitted behind it would be a second full scan.
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        self.addCleanup(lambda: lock.locked() and lock.release())
        calls: list[int] = []

        def scan(vault, *, _on_start=None):
            if _on_start:
                _on_start()
            calls.append(1)
            return {"local": 1, "cloud": 1}

        async def register_watcher():
            return await storage._reconcile_vault_after_watcher({"id": VAULT_ID})

        with patch.object(storage, "scan_vault", side_effect=scan):
            future = storage.queue_scan({"id": VAULT_ID})
            self.assertIs(storage._submit_watcher_scan({"id": VAULT_ID}), future)
            loop = asyncio.new_event_loop()
            self.addCleanup(loop.close)
            watcher = loop.create_task(register_watcher())
            loop.call_soon(lock.release)
            outcome = loop.run_until_complete(asyncio.wait_for(watcher, 5))
            self.assertEqual(future.result(timeout=5), {"local": 1, "cloud": 1})
        self.assertEqual(outcome, storage._WatcherScanOutcome())
        self.assertEqual(calls, [1], "one full scan for the queued request and the watcher")

    def test_a_request_that_cannot_be_submitted_is_not_left_pending(self):
        with patch.object(storage, "_submit_queued_scan", side_effect=RuntimeError("no thread")):
            future = storage.queue_scan({"id": VAULT_ID})
        self.assertIsInstance(future.exception(timeout=1), RuntimeError)
        self.assertNotIn(VAULT_ID, storage._queued_scans)
        self.assertFalse(snapshot_runtime_status_for_stats(VAULT_ID)["scan_queued"])
        with patch.object(storage, "_submit_queued_scan"):
            self.assertIsNot(storage.queue_scan({"id": VAULT_ID}), future)

    def test_the_shutdown_drain_awaits_a_waiting_scan(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        self.addCleanup(lock.release)
        state = MagicMock(requested=False)
        with patch.object(storage, "shutdown_state", state), \
                patch.object(storage, "scan_vault") as scan:
            future = storage.queue_scan({"id": VAULT_ID})
            self.assertTrue(settle(lambda: any(task.running() for task in storage._watcher_scan_futures)))
            tasks = storage._detach_background_executors()
            self.assertEqual(len(tasks), 1)
            state.requested = True
            tasks[0].result(timeout=5)
            self.assertEqual(future.result(timeout=5), {"stopped": 1})
        scan.assert_not_called()

    def test_a_waiting_scan_dropped_by_the_shutdown_before_it_ran_is_settled(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        self.addCleanup(lock.release)
        gate = threading.Event()
        self.addCleanup(gate.set)
        # The Vault's scan thread is busy, so the waiting scan is still queued.
        storage._watcher_scan_executor(VAULT_ID).submit(gate.wait, 5)
        future = storage.queue_scan({"id": VAULT_ID})
        storage._detach_background_executors()
        self.assertEqual(future.result(timeout=5), {"stopped": 1})
        self.assertNotIn(VAULT_ID, storage._queued_scans)


class VerificationReadProgressTests(unittest.TestCase):
    """A large file read for minutes still moves (#759)."""

    def test_the_bytes_read_are_counted_per_chunk_before_the_file_ends(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mkv"
            path.write_bytes(b"v" * (9 * 1024 * 1024))
            info = path.stat()
            row = {"path": "video.mkv", "vault_file_id": "f1", "generation": 1,
                   "size": info.st_size, "mtime_ns": info.st_mtime_ns}
            chunks: list[int] = []
            kind, _update = storage._hash_local_copy(Path(directory), row, on_read=chunks.append)
        self.assertEqual(kind, "hashed")
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(chunks), 9 * 1024 * 1024)

    def test_the_scan_publishes_the_bytes_read_in_its_runtime_status(self):
        self.addCleanup(runtime.runtime_status.pop, VAULT_ID, None)
        runtime.runtime_status[VAULT_ID] = {"scanning": True, "scan_id": "s"}
        seen: list[int] = []

        def fingerprints(vault, root, rows, *, scan_journal=None, on_result=None, on_read=None):
            for size in (1000, 2000):
                on_read(size)
                seen.append(runtime.runtime_status[VAULT_ID]["verify_bytes_read"])
            # No file completed yet: the counters of completed files stay put.
            self.assertEqual(runtime.runtime_status[VAULT_ID]["verify_bytes_done"], 0)
            return (update for update in ())

        with patch.object(storage, "_unfingerprinted_local_rows",
                          return_value=[{"path": "a", "size": 3000}]), \
                patch.object(storage, "_iter_local_fingerprints", side_effect=fingerprints):
            storage._fingerprint_scanned_local_copies({"id": VAULT_ID}, None, "s", scan_journal={})
        self.assertEqual(seen, [1000, 3000])


class StalledVaultTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(runtime.scan_locks.pop, VAULT_ID, None)
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []

        @contextmanager
        def db():
            yield connection

        for patcher in (
            patch.object(storage, "db", db),
            patch.object(storage, "_stalled_vaults", None),
            patch.dict(storage._stalled_vaults_refreshed_at, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.problem = patch.object(storage.notification_service, "set_problem").start()
        self.addCleanup(patch.stopall)

    def calls(self):
        return [(call.kwargs["key"], call.kwargs["active"], call.kwargs.get("params"))
                for call in self.problem.call_args_list]

    def test_background_work_over_ten_minutes_opens_a_problem_that_closes_when_it_ends(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="auto_admission")
        lock.relabel("identify_local_copies")
        since = lock.holder()["since_monotonic"]
        # Nine minutes: still working, nothing reported.
        self.assertEqual(storage.observe_vault_stalls(now=since + 540), {})
        self.assertEqual(self.calls(), [])
        stalled = storage.observe_vault_stalls(now=since + storage.VAULT_STALL_SECONDS + 1)
        self.assertEqual(list(stalled), [VAULT_ID])
        key, active, params = self.calls()[0]
        self.assertEqual((key, active), (f"vault_stalled:{VAULT_ID}", True))
        self.assertEqual(params["operation_key"], "identify_local_copies")
        self.assertEqual(params["held_since"], lock.holder()["since"])
        self.assertEqual(params["minutes"], 10)
        self.assertFalse(self.problem.call_args.kwargs["email"])
        # The next check within a minute writes nothing; after it, the duration.
        storage.observe_vault_stalls(now=since + storage.VAULT_STALL_SECONDS + 30)
        self.assertEqual(len(self.calls()), 1)
        storage.observe_vault_stalls(now=since + storage.VAULT_STALL_SECONDS + 62)
        self.assertEqual(self.calls()[-1][2]["minutes"], 11)
        lock.release()
        self.assertEqual(storage.observe_vault_stalls(now=since + 700), {})
        self.assertEqual(self.calls()[-1][:2], (f"vault_stalled:{VAULT_ID}", False))
        self.assertFalse(self.problem.call_args.kwargs["email"])

    def test_a_long_full_scan_is_never_a_stalled_vault(self):
        lock = scan_lock_for_vault(VAULT_ID)
        lock.acquire(operation="full_scan")
        try:
            since = lock.holder()["since_monotonic"]
            self.assertEqual(storage.observe_vault_stalls(now=since + 3600), {})
        finally:
            lock.release()
        self.assertEqual(self.calls(), [])

    def test_the_first_check_closes_a_problem_left_open_by_a_previous_process(self):
        with patch.object(storage, "lock_holders", return_value={}):
            connection = MagicMock()
            connection.execute.return_value.fetchall.return_value = [{"vault_id": 12}]

            @contextmanager
            def db():
                yield connection

            with patch.object(storage, "db", db):
                storage.observe_vault_stalls(now=0.0)
                storage.observe_vault_stalls(now=1.0)
        self.assertEqual(self.calls(), [("vault_stalled:12", False, None)])


if __name__ == "__main__":
    unittest.main()
