"""Real uvicorn + SIGTERM regression coverage, without PostgreSQL or Docker."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ServerShutdownTests(unittest.TestCase):
    def test_sigterm_with_open_sse_exits_cleanly_before_deadline(self):
        self._stop_with_open_stream(signal.SIGTERM)

    def test_sigint_with_open_sse_exits_cleanly(self):
        self._stop_with_open_stream(signal.SIGINT)

    def test_sigterm_drains_active_managed_upload_and_retains_late_receipt(self):
        self._stop_with_open_stream(signal.SIGTERM, worker="upload")

    def _stop_with_open_stream(self, stop_signal, *, worker=""):
        # Pass an already-bound loopback socket: no free-port race or public bind.
        with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as log:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            process = subprocess.Popen([
                sys.executable, "-m", "app.server", "tests.shutdown_fixture:app",
                "--fd", str(listener.fileno()), "--no-access-log",
            ], cwd=ROOT, pass_fds=(listener.fileno(),), stdout=log, stderr=log,
                env={**os.environ, "PYTHONUNBUFFERED": "1",
                     "FROSTVAULT_SHUTDOWN_FIXTURE_WORKER": worker})
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
                    self.assertEqual(result, 0, output)
                    self.assertLess(elapsed, 3, output)
                    self.assertIn("fixture: lifespan shutdown complete", output)
                    self.assertEqual(output.count("fixture: producer stopped"), 5, output)
                    if worker:
                        self.assertIn("upload drained; late receipt retained; queued work cancelled", output)
                    self.assertNotIn("timeout graceful shutdown exceeded", output)
                    self.assertNotIn("ERROR", output)
                    # HTTP chunked EOF, not an abrupt reset/cancelled response.
                    while chunk := stream.recv(4096):
                        received += chunk
                    self.assertTrue(received.endswith(b"0\r\n\r\n"), received)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
