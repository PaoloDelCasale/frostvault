"""FrostVault's single-process uvicorn entrypoint with early cooperative stop.

``python -m app.server`` is the container/native production entrypoint. A plain
``uvicorn app.main:app`` cannot notify SSE/worker producers before uvicorn drains
HTTP connections. Keep that ordering at the server boundary, not in lifespan.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import logging
import os
import signal
import socket
import threading
from collections.abc import Iterator

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from .services.shutdown import HTTP_DRAIN_SECONDS, PROCESS_SHUTDOWN_SECONDS, shutdown_state


logger = logging.getLogger("uvicorn.error")


class FrostVaultServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)
        # Unlike uvicorn's CLI, do not re-raise a handled SIGTERM/SIGINT after
        # graceful shutdown: the container contract is a successful exit (0).
        # handle_exit still supports a second SIGINT to force HTTP shutdown.

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # asyncio cancellation cannot stop synchronous SDK/filesystem calls or
        # Python's executor atexit joins. Bound the WHOLE process, not just HTTP.
        # Never report success if an uncooperative worker exceeded its budget.
        def deadline_exceeded() -> None:
            logger.error("Process shutdown deadline exceeded; unfinished work retains durable recovery evidence")
            os._exit(1)

        deadline = threading.Timer(PROCESS_SHUTDOWN_SECONDS, deadline_exceeded)
        deadline.daemon = True
        deadline.start()
        shutdown_state.request()
        # Leave this daemon watchdog armed through asyncio.run's default-pool
        # drain and interpreter executor joins. A clean process exits first.
        await super().shutdown(sockets=sockets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?", default="app.main:app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--fd", type=int)
    parser.add_argument("--no-access-log", action="store_true")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--forwarded-allow-ips", default=None)
    parser.add_argument("--timeout-graceful-shutdown", type=int, default=HTTP_DRAIN_SECONDS)
    args = parser.parse_args()
    if not 0 < args.timeout_graceful_shutdown <= HTTP_DRAIN_SECONDS:
        parser.error(f"--timeout-graceful-shutdown must be between 1 and {HTTP_DRAIN_SECONDS}")
    server = FrostVaultServer(uvicorn.Config(
        args.app, host=args.host, port=args.port, fd=args.fd,
        workers=1, access_log=not args.no_access_log, log_level=args.log_level,
        forwarded_allow_ips=args.forwarded_allow_ips,
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
    ))
    server.run()
    if not server.started:
        raise SystemExit(3)
    if getattr(server.lifespan, "shutdown_failed", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
