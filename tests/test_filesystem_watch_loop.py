"""What the filesystem watcher asks of the worker, and when (#795).

The watcher loop keeps one watcher per active Vault; one watcher run costs a
fixed number of thread hops, catalog connections and calls per cycle. Both are
fixed here through the names the worker gives the watcher, with no database
and with a scripted observer in place of the child process.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchfiles import Change

from app import storage
from app.services import watch_plan
from tests.fixtures import override_settings

ALLOWED = SimpleNamespace(local_operations_allowed=True)
BLOCKED = SimpleNamespace(local_operations_allowed=False)


class WatcherCase(unittest.IsolatedAsyncioTestCase):
    """The worker's side of the watcher replaced by recorders."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        # Ordered record of what the watcher did, by the worker's name for it.
        self.calls: list[tuple] = []
        self.hops = 0
        self.connections = 0
        self.sleeps: list[float] = []
        # Paths of the observers that are open now, and of every one ever opened.
        self.open: list[tuple[str, ...]] = []
        self.opened: list[tuple[str, ...]] = []
        self.options: list[dict] = []
        self.script = None
        self.access = {}

        def recorded(name, result=None):
            def record(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return result(*args, **kwargs) if callable(result) else result
            return self.stack.enter_context(patch(f"app.storage.{name}", side_effect=record))

        database = self.stack.enter_context(patch("app.storage.db"))
        database.side_effect = self.connect
        self.connection = database.return_value.__enter__.return_value
        self.connection.execute.return_value.fetchone.return_value = None
        self.stack.enter_context(patch(
            "app.storage.source_layout.vault_local_access",
            side_effect=lambda root: self.access.get(root, ALLOWED),
        ))
        self.stack.enter_context(patch("app.storage.source_layout.verify_mounts_once"))
        self.stack.enter_context(override_settings(
            filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=40,
            filesystem_watch_force_polling=False))
        self.stack.enter_context(patch("app.storage.awatch", side_effect=self.observe))
        real_to_thread = asyncio.to_thread

        async def counted(function, *args, **kwargs):
            self.hops += 1
            return await real_to_thread(function, *args, **kwargs)

        self.stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=counted))
        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *args):
            self.sleeps.append(delay)
            await real_sleep(min(delay, 0.01))

        self.real_sleep = real_sleep
        self.stack.enter_context(patch("app.storage.asyncio.sleep", side_effect=fast_sleep))
        recorded("_resolve_worker_error")
        recorded("_report_worker_error")
        recorded("_record_scan_component_health")
        recorded("_apply_watcher_changes", 1)
        recorded("_unknown_watcher_directories", lambda *_args: set())
        recorded("scan_vault", {"local": 1, "cloud": 1})
        self.stack.enter_context(patch("app.storage.worker_error_store.log_worker_error"))
        self.addCleanup(storage._shutdown_watcher_scan_executor)

    def connect(self, *args, **kwargs):
        self.connections += 1
        return storage.db.return_value

    def observe(self, *paths, **options):
        """A quiet observer; ``self.script`` gives the first one its batches."""
        registered = tuple(str(path) for path in paths)
        self.opened.append(registered)
        self.options.append(options)
        script, self.script = self.script, None

        async def generate():
            self.open.append(registered)
            try:
                if script is not None:
                    async for batch in script():
                        yield batch
                while True:
                    yield set()
                    await self.real_sleep(0.005)
            finally:
                self.open.remove(registered)

        return generate()

    async def settle(self, condition, timeout=5):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            self.assertLess(asyncio.get_running_loop().time(), deadline, "condition not reached")
            await self.real_sleep(0.005)

    def names(self):
        return [name for name, _args, _kwargs in self.calls]

    def vault(self, vault_id, name, **fields):
        root = self.directory / name
        root.mkdir(exist_ok=True)
        self.addCleanup(watch_plan.forget, vault_id)
        self.addCleanup(storage.runtime_status.pop, vault_id, None)
        return {"id": vault_id, "source_root": str(root), "root_identity": "inode-a",
                "reconnect_volume_fingerprint": "mount-a", **fields}


class WatchLoopTests(WatcherCase):
    """``filesystem_watch_loop``: one watcher per active Vault, kept aligned."""

    def setUp(self):
        super().setUp()
        self.vaults: list[dict] = []
        self.stack.enter_context(patch("app.storage._enabled_vaults", side_effect=lambda: list(self.vaults)))

    async def run_loop(self):
        task = asyncio.create_task(storage.filesystem_watch_loop())

        async def stop():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    async def test_one_watcher_per_active_vault_and_shutdown_closes_them(self):
        first, second = self.vault(9501, "a"), self.vault(9502, "b")
        self.vaults = [first, second]
        task = await self.run_loop()
        await self.settle(lambda: len(self.open) == 2)
        self.assertEqual(sorted(self.open), [(first["source_root"],), (second["source_root"],)])
        self.assertEqual(self.options[0], dict(
            watch_filter=storage._filesystem_watch_filter, debounce=30, force_polling=False,
            poll_delay_ms=40, ignore_permission_denied=False, yield_on_timeout=True,
            rust_timeout=1000, recursive=True,
        ))
        watch_filter = self.options[0]["watch_filter"]
        self.assertTrue(watch_filter(Change.added, "/v/photo.jpg"))
        self.assertFalse(watch_filter(Change.added, f"/v/.photo.jpg.restore-{'a' * 32}.tmp.f8Fc29bA"))
        # The loop looks again every ten seconds, and starts nothing twice.
        await self.settle(lambda: self.sleeps.count(10) >= 3)
        self.assertEqual(len(self.opened), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.open, [])

    async def test_a_vault_that_leaves_the_list_loses_its_watcher(self):
        first, second = self.vault(9503, "a"), self.vault(9504, "b")
        self.vaults = [first, second]
        await self.run_loop()
        await self.settle(lambda: len(self.open) == 2)
        self.vaults = [second]
        await self.settle(lambda: self.open == [(second["source_root"],)])
        self.assertEqual(len(self.opened), 2)

    async def test_a_changed_root_identity_or_mount_starts_a_new_watcher(self):
        vault = self.vault(9505, "a")
        self.vaults = [vault]
        await self.run_loop()
        await self.settle(lambda: len(self.open) == 1)
        self.vaults = [{**vault, "root_identity": "inode-b"}]
        await self.settle(lambda: len(self.opened) == 2 and len(self.open) == 1)
        self.vaults = [{**vault, "root_identity": "inode-b", "reconnect_volume_fingerprint": "mount-b"}]
        await self.settle(lambda: len(self.opened) == 3 and len(self.open) == 1)

    async def test_a_relocating_vault_is_not_watched(self):
        vault = self.vault(9506, "a")
        self.vaults = [vault]
        suspended = self.stack.enter_context(patch(
            "app.storage.vault_relocation.local_work_suspended", return_value=False))
        await self.run_loop()
        await self.settle(lambda: len(self.open) == 1)
        suspended.return_value = True
        await self.settle(lambda: self.open == [])
        ticks = self.sleeps.count(10)
        await self.settle(lambda: self.sleeps.count(10) >= ticks + 2)
        self.assertEqual(len(self.opened), 1)
        self.assertNotIn("_record_scan_component_health", self.names())

    async def test_an_unavailable_source_volume_is_reported_and_not_watched(self):
        vault = self.vault(9507, "a")
        self.vaults = [vault]
        await self.run_loop()
        await self.settle(lambda: len(self.open) == 1)
        self.access[vault["source_root"]] = BLOCKED
        await self.settle(lambda: self.open == [])
        self.assertIn(
            ("_record_scan_component_health", (9507, "source"),
             {"ok": False, "error": "Protection blocked: Source Volume unavailable"}),
            self.calls,
        )
        del self.access[vault["source_root"]]
        await self.settle(lambda: len(self.open) == 1)
        self.assertEqual(len(self.opened), 2)


class WatcherCycleCostTests(WatcherCase):
    """One watcher run: what a cycle costs, idle and with one event."""

    async def run_script(self, vault, batches, *, until=None):
        """The cost of each batch but the last: (thread hops, connections, calls).

        A cost is read when the observer is asked for the next batch, so the
        last batch only ends the script (after ``until``, when given).
        """
        marks: list[tuple[int, int, list[str]]] = []
        finished = asyncio.Event()

        async def script():
            for batch in batches:
                marks.append((self.hops, self.connections, self.names()))
                yield batch
            finished.set()

        self.script = script
        task = asyncio.create_task(storage._watch_vault_filesystem(vault))
        try:
            if until is None:
                await asyncio.wait_for(finished.wait(), 5)
            else:
                await self.settle(until)
                marks.append((self.hops, self.connections, self.names()))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        costs = []
        for previous, (hops, connections, names) in zip(marks, marks[1:]):
            costs.append((hops - previous[0], connections - previous[1], names[len(previous[2]):]))
        return costs

    async def test_start_idle_cycle_and_one_event(self):
        vault = self.vault(9511, "a")
        changed = str(Path(vault["source_root"]) / "photo.jpg")
        Path(changed).write_bytes(b"x")
        start, idle, event, after = await self.run_script(
            vault, [set(), set(), {(Change.modified, changed)}, set(), set()],
        )
        # The first healthy cycle closes the watcher's problem and pairs the
        # watcher with a scan; before it, one lifecycle read and the Source
        # Volume gate (the only connection and the other thread hop of the run).
        self.assertEqual(start[:2], (1, 0))
        self.assertEqual(
            [name for name in start[2] if name != "scan_vault"], ["_resolve_worker_error"],
        )
        self.assertEqual(self.calls[0], ("_resolve_worker_error", ("source_watcher", 9511), {}))
        self.assertEqual(self.connection.execute.call_args_list[0].args,
                         ("SELECT lifecycle FROM vaults WHERE id=%s", (9511,)))
        # An idle cycle asks nothing of the worker.
        self.assertEqual(idle[:2], (0, 0))
        self.assertEqual([name for name in idle[2] if name != "scan_vault"], [])
        # One event: new folders, folders the catalog does not know, the batch.
        self.assertEqual(event[:2], (3, 0))
        self.assertEqual(
            [name for name in event[2] if name != "scan_vault"],
            ["_unknown_watcher_directories", "_apply_watcher_changes"],
        )
        self.assertEqual(after[:2], (0, 0))
        applied = [call for call in self.calls if call[0] == "_apply_watcher_changes"]
        self.assertEqual(applied, [
            ("_apply_watcher_changes", (vault, {(Change.modified, changed)}), {"wait": False}),
        ])
        unknown = [call for call in self.calls if call[0] == "_unknown_watcher_directories"]
        self.assertEqual(unknown[0][1][:3], (9511, Path(vault["source_root"]), {(Change.modified, changed)}))
        self.assertEqual([call[1] for call in self.calls if call[0] == "scan_vault"], [(vault,)])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.opened, [(vault["source_root"],)])
        self.assertEqual((self.hops, self.connections), (5, 1))

    async def test_a_new_folder_registers_a_successor_before_the_batch(self):
        vault = self.vault(9512, "a")
        folder = Path(vault["source_root"]) / "album"
        folder.mkdir()
        _start, event, handoff = await self.run_script(
            vault, [set(), {(Change.added, str(folder))}, set()],
            until=lambda: "_apply_watcher_changes" in self.names(),
        )
        # New folders and unknown folders, then the successor registers and the
        # old observer is drained (the script's third batch) ...
        self.assertEqual(event[:2], (2, 0))
        self.assertEqual([name for name in event[2] if name != "scan_vault"],
                         ["_unknown_watcher_directories"])
        # ... both checks again on what arrived meanwhile, then the batch.
        self.assertEqual(handoff[:2], (3, 0))
        self.assertEqual(
            [name for name in handoff[2] if name != "scan_vault"],
            ["_unknown_watcher_directories", "_apply_watcher_changes"],
        )
        self.assertEqual(self.opened, [(vault["source_root"],)] * 2)
        self.assertEqual(self.open, [])

    async def test_an_observer_failure_is_reported_and_the_watcher_registers_again(self):
        vault = self.vault(9513, "a")
        failures = iter([RuntimeError("observer died")])

        async def script():
            yield set()
            raise next(failures)

        self.script = script
        task = asyncio.create_task(storage._watch_vault_filesystem(vault))
        try:
            await self.settle(lambda: len(self.opened) == 2 and "_report_worker_error" in self.names())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        reported = [call for call in self.calls if call[0] == "_report_worker_error"]
        self.assertEqual([(call[1][0], str(call[1][1]), call[1][2]) for call in reported],
                         [("source_watcher", "observer died", 9513)])
        self.assertEqual(storage.runtime_status[9513]["last_error"], "Source watcher: observer died")
        # The growing pause of the first failure, and nothing else slept.
        self.assertEqual(self.sleeps, [float(storage.WATCHER_RESTART_BASE_SECONDS)])
        self.assertEqual(self.open, [])

    async def test_a_blocked_source_volume_waits_and_reports(self):
        vault = self.vault(9514, "a")
        self.access[vault["source_root"]] = BLOCKED
        task = asyncio.create_task(storage._watch_vault_filesystem(vault))
        try:
            await self.settle(lambda: self.sleeps.count(5) >= 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.calls[0], (
            "_record_scan_component_health", (9514, "source"),
            {"ok": False, "error": "Protection blocked: Source Volume unavailable"},
        ))

    async def test_a_vault_that_is_not_active_waits_without_touching_the_volume(self):
        vault = self.vault(9515, "a")
        self.connection.execute.return_value.fetchone.return_value = {"lifecycle": "stopped"}
        task = asyncio.create_task(storage._watch_vault_filesystem(vault))
        try:
            await self.settle(lambda: self.sleeps.count(1) >= 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((self.hops, self.opened, self.calls), (0, [], []))


if __name__ == "__main__":
    unittest.main()
