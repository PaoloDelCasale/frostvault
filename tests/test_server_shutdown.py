"""Real uvicorn + SIGTERM regression coverage, without PostgreSQL or Docker."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from app import storage
from app.services.catalog_event_hub import catalog_event_hub
from app.services.catalog_event_stream import StreamTickSnapshot, iter_catalog_event_sse
from app.services.shutdown import (
    HTTP_DRAIN_SECONDS, PROCESS_SHUTDOWN_SECONDS, WORKER_DRAIN_SECONDS,
    WorkerStopping, shutdown_state,
)


ROOT = Path(__file__).resolve().parents[1]


class ServerShutdownTests(unittest.TestCase):
    def test_sigterm_with_open_sse_exits_cleanly_before_deadline(self):
        self._stop_with_open_stream(signal.SIGTERM)

    def test_sigint_with_open_sse_exits_cleanly(self):
        self._stop_with_open_stream(signal.SIGINT)

    def test_sigterm_drains_active_managed_upload_and_retains_late_receipt(self):
        self._stop_with_open_stream(signal.SIGTERM, worker="upload")

    def test_process_deadline_bounds_uncooperative_executor_threads(self):
        for worker in ("stuck-operation", "stuck-default"):
            with self.subTest(worker=worker):
                self._stop_with_open_stream(signal.SIGTERM, worker=worker, expected_exit=1)

    def _stop_with_open_stream(self, stop_signal, *, worker="", expected_exit=0):
        # Pass an already-bound loopback socket: no free-port race or public bind.
        with socket.socket() as listener, tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryFile(mode="w+") as log:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            launch = (["-c", "from app import server; server.PROCESS_SHUTDOWN_SECONDS = 1; server.main()"]
                      if expected_exit else ["-m", "app.server"])
            process = subprocess.Popen([
                sys.executable, *launch, "tests.shutdown_fixture:app",
                "--fd", str(listener.fileno()), "--no-access-log",
            ], cwd=ROOT, pass_fds=(listener.fileno(),), stdout=log, stderr=log,
                env={**os.environ, "PYTHONUNBUFFERED": "1",
                     "FROSTVAULT_SHUTDOWN_FIXTURE_WORKER": worker, "TMPDIR": workdir})
            listener.close()
            try:
                startup_deadline = time.monotonic() + 10
                while True:
                    try:
                        stream = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= startup_deadline:
                            log.seek(0)
                            self.fail(f"Server failed to start: {log.read()}")
                        time.sleep(0.02)
                with stream:
                    stream.settimeout(3)
                    stream.sendall(b"GET /api/catalog/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    received = b""
                    while b"event: hello" not in received:
                        chunk = stream.recv(4096)
                        self.assertTrue(chunk, "SSE ended before its hello frame")
                        received += chunk
                    self.assertIn(b"200 OK", received)
                    started = time.monotonic()
                    process.send_signal(stop_signal)
                    # Intentionally keep the client connection OPEN until exit.
                    try:
                        result = process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        log.seek(0)
                        self.fail(f"SIGTERM blocked on an active SSE connection:\n{log.read()}")
                    elapsed = time.monotonic() - started
                    log.seek(0)
                    output = log.read()
                    self.assertEqual(result, expected_exit, output)
                    self.assertLess(elapsed, 3, output)
                    self.assertEqual(output.count("fixture: producer stopped"), 5, output)
                    if expected_exit:
                        self.assertIn("Process shutdown deadline exceeded", output)
                    else:
                        self.assertIn("fixture: lifespan shutdown complete", output)
                        self.assertNotIn("ERROR", output)
                    if worker == "upload":
                        self.assertIn("upload drained; late receipt retained; queued work cancelled", output)
                    self.assertNotIn("timeout graceful shutdown exceeded", output)
                    # HTTP chunked EOF, not an abrupt reset/cancelled response.
                    while chunk := stream.recv(4096):
                        received += chunk
                    self.assertTrue(received.endswith(b"0\r\n\r\n"), received)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


class StreamShutdownBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_wakes_hub_and_hubless_streams_without_another_db_tick(self):
        for use_hub in (True, False):
            with self.subTest(use_hub=use_hub):
                shutdown_state.reset()
                snapshot = StreamTickSnapshot(ok=True, signal={
                    "vault_id": 1, "revision": 0, "domains": [], "has_gap": False,
                })
                with patch("app.services.catalog_event_stream.stream_tick_snapshot", return_value=snapshot) as tick:
                    stream = iter_catalog_event_sse(vault_id=1, user_id=1, session_id="test",
                        use_hub=use_hub, durable_poll_seconds=3600)
                    self.assertIn("event: hello", await anext(stream))
                    waiting = asyncio.create_task(anext(stream))
                    await asyncio.sleep(0)
                    shutdown_state.request()
                    with self.assertRaises(StopAsyncIteration):
                        await asyncio.wait_for(waiting, 0.5)
                    tick.assert_called_once()
                self.assertEqual(catalog_event_hub.subscriber_count(), 0)
        shutdown_state.reset()

    async def test_disconnected_stream_reaps_queue_and_shutdown_waiters(self):
        shutdown_state.reset()
        self.addCleanup(shutdown_state.reset)
        with patch("app.services.catalog_event_stream.stream_tick_snapshot", return_value=StreamTickSnapshot(ok=True)):
            stream = iter_catalog_event_sse(vault_id=1, user_id=1, session_id="test", durable_poll_seconds=3600)
            await anext(stream)
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.01)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(catalog_event_hub.subscriber_count(), 0)
            self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})


class WorkerDrainBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        shutdown_state.reset()
        self.addCleanup(shutdown_state.reset)
        self.addCleanup(storage.shutdown_background_executors)

    async def test_stop_refuses_admission_and_preserves_job_state_without_db_io(self):
        shutdown_state.request()
        with patch("app.storage.db", side_effect=AssertionError("unexpected DB I/O")), patch("app.storage.set_job") as terminal:
            self.assertEqual(storage.process_jobs_once(wait=False), 0)
            self.assertFalse(storage.process_job({"id": 1, "action": "upload"}))
            with self.assertRaises(WorkerStopping):
                storage.ensure_job_active(1, "stop")
            with self.assertRaises(WorkerStopping):
                storage.ensure_job_claim_owned_in_transaction(None, {"id": 1}, "stop")
            with self.assertRaises(WorkerStopping):
                storage._operation_pool(1)
            storage._submit_maintenance("must-not-start", lambda: self.fail("maintenance admitted"))
            self.assertEqual(storage._maintenance_futures, {})
            terminal.assert_not_called()

    async def test_worker_drain_timeout_returns_without_blocking_event_loop(self):
        release = threading.Event()
        entered = threading.Event()
        def blocking_worker():
            entered.set()
            release.wait(5)
        future = storage._operation_pool(1).submit(blocking_worker)
        storage._operation_futures[1] = future
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            started = time.monotonic()
            self.assertFalse(await storage.drain_background_executors(timeout=0.02))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(future.done())
        finally:
            release.set()
            await asyncio.to_thread(future.result, 1)


class DeploymentShutdownBudgetTests(unittest.TestCase):
    def test_image_and_compose_share_a_bounded_shutdown_budget(self):
        command_line = next(line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("CMD "))
        command = json.loads(command_line.removeprefix("CMD "))
        self.assertEqual(command[:4], ["python", "-m", "app.server", "app.main:app"])
        self.assertEqual(int(command[command.index("--timeout-graceful-shutdown") + 1]), HTTP_DRAIN_SECONDS)
        self.assertLess(HTTP_DRAIN_SECONDS + WORKER_DRAIN_SECONDS, PROCESS_SHUTDOWN_SECONDS)
        for manifest in ("compose.yaml", "compose.traefik.yaml"):
            with self.subTest(manifest=manifest):
                service = yaml.safe_load((ROOT / manifest).read_text())["services"]["frostvault"]
                period = service["stop_grace_period"]
                self.assertTrue(period.endswith("s"))
                self.assertGreater(int(period[:-1]), PROCESS_SHUTDOWN_SECONDS)

    def test_entrypoint_recognizes_new_server_without_migrating_one_shot_commands(self):
        # Execute ONLY the pure command classifier, never the entrypoint's
        # runtime identity/directory/migration operations on this host.
        text = (ROOT / "docker/entrypoint.sh").read_text()
        classifier = text.split("is_web_command() {", 1)[1].split("\n}\n", 1)[0]
        shell = "is_web_command() {" + classifier + '\n}\nis_web_command "$@"'
        for command, expected in ((["python", "-m", "app.server"], 0),
                                  (["uvicorn", "app.main:app"], 0),
                                  (["python", "-m", "app.backup_upgrade"], 1),
                                  (["python"], 1), (["sh"], 1), ([], 1)):
            with self.subTest(command=command):
                result = subprocess.run(["sh", "-c", shell, "classifier", *command], capture_output=True)
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
