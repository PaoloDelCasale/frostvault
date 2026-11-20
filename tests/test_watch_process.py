"""The native observer in a child process (ADR-0053, #777).

Real child processes and real temporary trees: the forwarded events and
errors are compared with watchfiles' in-process observer.
"""
import asyncio
from contextlib import aclosing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from watchfiles import Change, awatch as inprocess_awatch

from app import storage
from app.services import watch_process

OPTIONS = dict(debounce=50, step=10, rust_timeout=200, yield_on_timeout=True)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _slow_child(directory: Path, seconds: float) -> Path:
    """A child whose registration holds its own GIL for ``seconds``."""
    script = directory / "slow_child.py"
    script.write_text(textwrap.dedent(f"""
        import runpy, time
        import watchfiles._rust_notify as rust

        real = rust.RustNotify

        def slow(*args):
            end = time.monotonic() + {seconds!r}
            while time.monotonic() < end:
                pass
            return real(*args)

        rust.RustNotify = slow
        runpy.run_path({str(watch_process.CHILD_SCRIPT)!r}, run_name="__main__")
    """))
    return script


class ObserverProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "vault"
        (self.root / "a" / "b").mkdir(parents=True)

    async def asyncTearDown(self):
        self.assertEqual(watch_process.live_observer_count(), 0)

    async def test_storage_uses_the_child_process_observer(self):
        self.assertIs(storage.awatch, watch_process.awatch)

    async def test_forwards_the_same_filtered_changes_as_awatch(self):
        def no_tmp(_change, path):
            return not path.endswith(".tmp")

        async with aclosing(watch_process.awatch(self.root, watch_filter=no_tmp, **OPTIONS)) as child, \
                aclosing(inprocess_awatch(self.root, watch_filter=no_tmp, **OPTIONS)) as local:
            self.assertEqual(await anext(child), set())
            self.assertEqual(await anext(local), set())
            (self.root / "a" / "b" / "kept.txt").write_text("x")
            (self.root / "a" / "ignored.tmp").write_text("x")
            (self.root / "a" / "c").mkdir()
            await asyncio.sleep(0.3)
            seen_child, seen_local = set(), set()
            for _ in range(5):
                seen_child |= await anext(child)
                seen_local |= await anext(local)
        expected = {
            (Change.added, str(self.root / "a" / "b" / "kept.txt")),
            (Change.added, str(self.root / "a" / "c")),
        }
        self.assertTrue(expected <= seen_child, seen_child)
        self.assertEqual(seen_child, seen_local)
        self.assertFalse(any(path.endswith(".tmp") for _change, path in seen_child))

    async def test_flat_observer_ignores_subdirectories(self):
        async with aclosing(watch_process.awatch(self.root, recursive=False, watch_filter=None, **OPTIONS)) as child:
            await anext(child)
            (self.root / "a" / "b" / "deep.txt").write_text("x")
            (self.root / "top.txt").write_text("x")
            await asyncio.sleep(0.3)
            seen = set()
            for _ in range(3):
                seen |= await anext(child)
        self.assertIn((Change.added, str(self.root / "top.txt")), seen)
        self.assertNotIn((Change.added, str(self.root / "a" / "b" / "deep.txt")), seen)

    async def test_a_large_batch_arrives_as_one_set(self):
        names = [self.root / f"file-{index}" for index in range(watch_process_chunk() + 500)]
        async with aclosing(watch_process.awatch(
            self.root, watch_filter=None, debounce=3000, step=500, rust_timeout=5000,
            yield_on_timeout=True,
        )) as child:
            await anext(child)
            await asyncio.to_thread(lambda: [name.write_bytes(b"") for name in names])
            batch = await anext(child)
        self.assertTrue({(Change.added, str(name)) for name in names} <= batch)

    async def test_missing_path_raises_like_awatch(self):
        missing = self.root / "missing"
        with self.assertRaises(FileNotFoundError) as from_child:
            async with aclosing(watch_process.awatch(missing, **OPTIONS)) as child:
                await anext(child)
        with self.assertRaises(FileNotFoundError) as from_local:
            async with aclosing(inprocess_awatch(missing, **OPTIONS)) as local:
                await anext(local)
        self.assertEqual(str(from_child.exception), str(from_local.exception))
        self.assertEqual(from_child.exception.filename, from_local.exception.filename)

    @unittest.skipIf(os.geteuid() == 0, "root enters any folder")
    async def test_denied_folder_is_named_like_awatch(self):
        denied = self.root / "a" / "denied"
        denied.mkdir()
        (denied / "inner").mkdir()
        denied.chmod(0)
        self.addCleanup(denied.chmod, 0o755)
        errors = []
        for observe in (watch_process.awatch, inprocess_awatch):
            with self.assertRaises(PermissionError) as raised:
                async with aclosing(observe(self.root, ignore_permission_denied=False, **OPTIONS)) as observer:
                    await anext(observer)
            errors.append(raised.exception)
        self.assertEqual(str(errors[0]), str(errors[1]))
        root = self.root.resolve()
        self.assertEqual(storage._watcher_denied_directories(errors[0], root), {"a/denied"})

    async def test_unknown_child_errors_keep_their_message(self):
        error = watch_process._rebuild_error(
            {"module": "watchfiles._rust_notify", "name": "WatchfilesRustInternalError",
             "args": ["Unable to decode path"], "message": "Unable to decode path"})
        self.assertEqual(type(error).__name__, "WatchfilesRustInternalError")
        self.assertTrue(storage._is_undecodable_path_error(error))
        error = watch_process._rebuild_error(
            {"module": "somewhere", "name": "Odd", "args": [], "message": "boom"})
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), "Odd: boom")
        error = watch_process._rebuild_error(
            {"module": "builtins", "name": "FileNotFoundError", "args": [2, "gone"],
             "message": "x", "errno": 2, "strerror": "gone", "filename": "/v/a"})
        self.assertEqual((error.errno, error.filename), (2, "/v/a"))
        self.assertEqual(storage._watcher_vanished_directories(error, Path("/v")), {"a"})

    async def test_event_loop_answers_while_the_child_registers(self):
        script = _slow_child(Path(self.directory.name), 2.0)
        ticks: list[float] = []

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        with patch.object(watch_process, "CHILD_SCRIPT", script):
            async with aclosing(watch_process.awatch(self.root, **OPTIONS)) as child:
                self.assertEqual(await anext(child), set())
        registered = time.monotonic() - started
        beat.cancel()
        gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
        self.assertGreaterEqual(registered, 2.0)
        self.assertGreater(len(ticks), 50)
        self.assertLess(max(gaps), 0.5)

    async def test_closing_or_cancelling_stops_the_child(self):
        script = _slow_child(Path(self.directory.name), 30.0)
        with patch.object(watch_process, "CHILD_SCRIPT", script):
            observer = watch_process.awatch(self.root, **OPTIONS)
            waiting = asyncio.create_task(anext(observer))
            while not watch_process._live:
                await asyncio.sleep(0.01)
            process = next(iter(watch_process._live))
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            await observer.aclose()
        self.assertIsNotNone(process.returncode)
        self.assertFalse(_alive(process.pid))

        async with aclosing(watch_process.awatch(self.root, **OPTIONS)) as observer:
            await anext(observer)
            process = next(iter(watch_process._live))
        self.assertFalse(_alive(process.pid))

    async def test_child_dies_with_the_server(self):
        script = _slow_child(Path(self.directory.name), 60.0)
        parent = Path(self.directory.name) / "parent.py"
        parent.write_text(textwrap.dedent(f"""
            import asyncio, sys
            from pathlib import Path
            sys.path.insert(0, {os.getcwd()!r})
            from app.services import watch_process
            watch_process.CHILD_SCRIPT = Path({str(script)!r})

            async def main():
                observer = watch_process.awatch({str(self.root)!r})
                waiting = asyncio.ensure_future(anext(observer))
                while not watch_process._live:
                    await asyncio.sleep(0.01)
                print(next(iter(watch_process._live)).pid, flush=True)
                await waiting

            asyncio.run(main())
        """))
        server = subprocess.Popen([sys.executable, str(parent)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(server.wait)
        child_pid = int(server.stdout.readline())
        server.stdout.close()
        self.assertTrue(_alive(child_pid))
        # While registering, the child cannot read its pipe: the kernel ends it.
        server.send_signal(signal.SIGKILL)
        deadline = time.monotonic() + 10
        while _alive(child_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(_alive(child_pid))


def watch_process_chunk() -> int:
    import importlib.util

    spec = importlib.util.spec_from_file_location("watch_child", watch_process.CHILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CHUNK


if __name__ == "__main__":
    unittest.main()
