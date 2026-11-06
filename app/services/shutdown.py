"""Process-local stop admission and cooperative worker/SSE shutdown.

Uvicorn must request this BEFORE waiting for HTTP connections, not from the
ASGI lifespan shutdown (which is reached only after those connections finish).
Async methods run on the server loop; worker threads only read ``requested``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import threading


HTTP_DRAIN_SECONDS = 5
WORKER_DRAIN_SECONDS = 20
PROCESS_SHUTDOWN_SECONDS = 30


class OperationCancelled(RuntimeError):
    """Raised when a queued or active operation is deliberately interrupted."""


class WorkerStopping(OperationCancelled):
    """Leave durable Job leases/evidence recoverable, not owner-cancelled.

    Use the existing cancellation hierarchy so transfer/verification/cleanup
    helpers propagate this unchanged instead of reclassifying it as bad proof.
    The Job runner handles this subtype before ordinary Owner cancellation.
    """


class ShutdownState:
    def __init__(self) -> None:
        self._requested = threading.Event()
        self._wake: asyncio.Event | None = None
        self._wake_loop: asyncio.AbstractEventLoop | None = None
        self._producers: list[asyncio.Task] = []
        self._stop_callbacks: list[Callable[[], None]] = []

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def reset(self) -> None:
        """Start a new lifespan (also supports repeated TestClient lifespans)."""
        self._requested.clear()
        self._wake = None
        self._wake_loop = None
        self._producers.clear()
        self._stop_callbacks.clear()

    def register_producers(self, tasks: list[asyncio.Task]) -> None:
        self._producers.extend(tasks)
        if self.requested:
            for task in tasks:
                task.cancel()

    def register_stop_callback(self, callback: Callable[[], None]) -> None:
        self._stop_callbacks.append(callback)
        if self.requested:
            callback()

    def request(self) -> None:
        if self.requested:
            return
        self._requested.set()
        if self._wake is not None:
            self._wake.set()
        for task in self._producers:
            task.cancel()
        for callback in self._stop_callbacks:
            callback()

    async def wait(self) -> None:
        if self.requested:
            return
        loop = asyncio.get_running_loop()
        if self._wake is None or self._wake_loop is not loop:
            self._wake = asyncio.Event()
            self._wake_loop = loop
        await self._wake.wait()

    def checkpoint(self) -> None:
        if self.requested:
            raise WorkerStopping("Process shutdown requested")


shutdown_state = ShutdownState()
