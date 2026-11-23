"""The one runner of the periodic workers (#795).

A periodic worker is declared once, as a ``PeriodicWorker``: its name, the
steps of one pass and its interval. Two things run a declared worker:

- ``run_forever`` paces the worker itself, as one task of the server loop;
- ``run_once`` runs one pass for a caller that owns the cadence (the
  background scheduler submits it to the maintenance pool).

Both record the worker's health here and nowhere else: a failed pass is
reported under the worker's name, a clean one notes the worker healthy. Where
the workers differ (their thread, whether a clean pass is noted, where the
report runs, what stopping waits for), the difference is a field of the
declaration.

This Module imports nothing of storage. ``WorkerHealth`` is what it asks of
its host; ``app.storage`` gives the production Adapter.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Protocol


class WorkerHealth(Protocol):
    """Where a worker's failures and recoveries are recorded."""

    def report_error(self, worker: str, exc: BaseException) -> None:
        """Record a failed pass of ``worker``; never raises."""

    def note_ok(self, worker: str) -> None:
        """A pass of ``worker`` ended cleanly: close its open failure, once."""


@dataclass(frozen=True)
class PeriodicWorker:
    # The component its health is recorded under.
    name: str
    # One pass: called in order; a step that raises ends the pass.
    steps: tuple[Callable[[], Any], ...]
    # Seconds between two passes, read at every pass.
    interval: Callable[[], float]
    # ``run_forever`` only. The worker's own single thread, by its name
    # prefix; without one, each step borrows a thread of the loop's default
    # pool.
    thread: str | None = None
    # A clean pass notes the worker healthy, on the thread that ran the steps.
    # Off for a worker whose pass says nothing about its health as a whole.
    notes_ok: bool = True
    # ``run_forever`` only. The failure is reported on the event loop's own
    # thread instead of a pool thread.
    reports_on_loop: bool = False
    # ``run_forever`` only, with ``thread``. Stopping waits for the running
    # pass instead of leaving it to finish by itself.
    drains_on_stop: bool = False
    # ``run_once`` only. The steps record the worker's health themselves (per
    # Vault, or to the log only): the runner records nothing and lets a
    # failure through.
    own_health: bool = False


# What a step returns when its pass had nothing to do and proves nothing: the
# pass ends there and the worker's health is left as it was.
NOT_RUN = object()


def run_once(worker: PeriodicWorker, health: WorkerHealth) -> None:
    """Run one pass on the calling thread and record its outcome."""
    if worker.own_health:
        for step in worker.steps:
            step()
        return
    try:
        for step in worker.steps:
            if step() is NOT_RUN:
                return
    except Exception as exc:
        health.report_error(worker.name, exc)
        return
    if worker.notes_ok:
        health.note_ok(worker.name)


async def run_forever(worker: PeriodicWorker, health: WorkerHealth) -> None:
    """Run pass after pass, an interval apart, until cancelled."""
    if worker.thread is None:
        await _passes(worker, health, asyncio.to_thread)
        return
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=worker.thread)
    try:
        await _passes(worker, health, partial(loop.run_in_executor, executor))
    finally:
        if worker.drains_on_stop:
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        else:
            executor.shutdown(wait=False, cancel_futures=True)


async def _passes(worker: PeriodicWorker, health: WorkerHealth, call: Callable[..., Any]) -> None:
    while True:
        try:
            for step in worker.steps:
                if await call(step) is NOT_RUN:
                    break
            else:
                if worker.notes_ok:
                    await call(health.note_ok, worker.name)
        except Exception as exc:
            if worker.reports_on_loop:
                health.report_error(worker.name, exc)
            else:
                await asyncio.to_thread(health.report_error, worker.name, exc)
        await asyncio.sleep(worker.interval())
