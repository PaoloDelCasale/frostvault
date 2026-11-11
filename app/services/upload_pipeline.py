"""Bounded stage ownership for already-claimed uploads, not another work queue.

The scheduler alone owns Job admission (1..16). Its foreground threads lend
capacity to hashing, transport buffers and DB-only commits here. There are no
payloads, receipts, Futures or unclaimed Jobs in this Module. A permit belongs to
one synchronous scope and is returned on *every* exit, including BaseException.

Defaults deliberately do not raise worker concurrency: two hash readers (4 MiB
of live read chunks), four sends (64 MiB nominal live payload), four catalog
units, at most two of those per Vault. All counts are capped by the installation's
operation_concurrency. The byte accounting is payload, not a promise about RSS
(Python/SDK/TLS allocations are additional). Classic multipart may require more
than 64 MiB for two parts: one separate oversized-buffer lane preserves that
immutable layout, bounded by two MAX_PART_SIZE parts plus read chunks. It shares
the SAME send-count permits, but cannot block small transfers behind an oversized
weight or occupy their 64 MiB budget for hours. Total transport payload capacity
is therefore 64 MiB + that one bounded large layout, NOT a 64 MiB RSS limit.
No tiny payload is captured or kept between stages. Snapshot metadata remains
bounded by 16 claimed Jobs x 10,000 checksums and dies with the attempt.

Receipt appends never acquire a catalog permit. A failed append closes *new*
send admission, without waiting behind catalog writers or occupying all workers
waiting for a recovery Job. A committed writable-journal probe can reopen admission for unrelated intents;
the durable send-started intent still MUST reconcile before another send. The
latch stores bounded failure identities, never a RAM receipt queue.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
import os
import threading
from typing import Any, Callable, Iterator

import psycopg

from .database_retry import owned_connection
from .upload_snapshot import CHUNK_SIZE, MAX_PART_SIZE, MULTIPART_THRESHOLD, UploadSnapshot


class UploadJournalHeld(psycopg.OperationalError):
    """Send admission is waiting, not a transfer failure or spent retry."""


@dataclass(frozen=True)
class _Limits:
    prepare_count: int = 2
    prepare_bytes: int = 4 * CHUNK_SIZE
    network_count: int = 4
    network_bytes: int = 64 * CHUNK_SIZE
    catalog_count: int = 4
    catalog_per_vault: int = 2


class _Capacity:
    """FIFO weighted permits; only claimed foreground threads can wait here."""

    def __init__(self, count: int, byte_limit: int = 1):
        if count < 1 or byte_limit < 1:
            raise ValueError("Upload capacity must be positive")
        self.count, self.byte_limit = count, byte_limit
        self._condition = threading.Condition()
        self._waiting: deque[object] = deque()
        self._count = self._bytes = 0

    @contextmanager
    def take(self, check: Callable[[], None], weight: int = 1) -> Iterator[None]:
        weight = max(1, weight)
        if weight > self.byte_limit:
            raise ValueError("Upload allocation exceeds its stage byte capacity")
        ticket = object()
        acquired = False
        with self._condition:
            self._waiting.append(ticket)
        try:
            while True:
                # Never call durable claim checks while holding this mutex.
                check()
                with self._condition:
                    if (self._waiting[0] is ticket and self._count < self.count
                            and self._bytes + weight <= self.byte_limit):
                        self._waiting.popleft()
                        self._count += 1
                        self._bytes += weight
                        acquired = True
                        self._condition.notify_all()
                        break
                    self._condition.wait(timeout=0.1)
            check()  # Cancellation/loss/shutdown winning while queued wins here.
            yield
        finally:
            with self._condition:
                if acquired:
                    self._count -= 1
                    self._bytes -= weight
                else:
                    self._waiting.remove(ticket)
                self._condition.notify_all()


class _Pipeline:
    def __init__(self, concurrency: int, limits: _Limits = _Limits()):
        concurrency = max(1, min(16, int(concurrency)))
        self.prepare = _Capacity(min(concurrency, limits.prepare_count), limits.prepare_bytes)
        network_count = min(concurrency, limits.network_count)
        self.network = _Capacity(network_count, network_count)
        self._buffers = _Capacity(network_count, limits.network_bytes)
        self._large_buffers = _Capacity(1, 2 * (MAX_PART_SIZE + CHUNK_SIZE))
        self.catalog = _Capacity(min(concurrency, limits.catalog_count), limits.catalog_count)
        self._vault_count = min(concurrency, limits.catalog_per_vault)
        self._lock = threading.Lock()
        self._vaults: dict[int, tuple[_Capacity, int]] = {}
        self._uncertain: dict[tuple[str, str], int] = {}
        self._journal_generation = 0

    @contextmanager
    def catalog_unit(self, job: dict[str, Any], check: Callable[[], None]) -> Iterator[None]:
        vault = int(job["vault_id"])
        with self._lock:
            entry = self._vaults.get(vault)
            capacity, users = entry if entry is not None else (_Capacity(self._vault_count, self._vault_count), 0)
            self._vaults[vault] = capacity, users + 1
        try:
            # Contention of one Vault cannot fill the global waiting queue or
            # occupy all four global slots. FIFO admission also bounds bypass
            # by later shared units; PostgreSQL retains its own writer fairness.
            # No connection/row lock exists until BOTH permits have been taken.
            with capacity.take(check), self.catalog.take(check):
                yield
        finally:
            with self._lock:
                capacity, users = self._vaults[vault]
                if users == 1:
                    del self._vaults[vault]
                else:
                    self._vaults[vault] = capacity, users - 1

    def journal_failed(self, intent_id: str, receipt_hash: str) -> None:
        with self._lock:
            self._journal_generation += 1
            self._uncertain[intent_id, receipt_hash] = self._journal_generation

    def journal_safe(self, intent_id: str, receipt_hash: str | None = None) -> None:
        with self._lock:
            if receipt_hash is None:  # Completed reconciliation, not any receipt.
                self._uncertain = {entry: generation for entry, generation in self._uncertain.items()
                                   if entry[0] != intent_id}
            else:
                self._uncertain.pop((intent_id, receipt_hash), None)

    def uncertain_journals(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._uncertain)

    def journal_probe_pending(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._uncertain)

    def journal_probe_committed(self, observed: dict[tuple[str, str], int]) -> None:
        with self._lock:
            for entry, generation in observed.items():
                # A probe confirms availability, NOT this receipt's durability.
                # Even the SAME fact failing again meanwhile is a newer hold.
                if self._uncertain.get(entry) == generation:
                    del self._uncertain[entry]

    def require_send_admission(self) -> None:
        with self._lock:
            if self._uncertain:
                # Release the claim with a timed wait, without spending transfer
                # retries or occupying all workers behind a reconciliation Job.
                raise UploadJournalHeld("Upload receipt journal unavailable; new sends held")

    @contextmanager
    def sending(self, snapshot: UploadSnapshot, check: Callable[[], None]) -> Iterator[None]:
        weight = (snapshot.size + 2 * CHUNK_SIZE if snapshot.size < MULTIPART_THRESHOLD
                  else 2 * (snapshot.part_size + CHUNK_SIZE))
        def active() -> None:
            check()
            self.require_send_admission()
        buffers = self._large_buffers if weight > self._buffers.byte_limit else self._buffers
        # Wait on the appropriate buffer lane BEFORE taking send count. Thus
        # a large-layout backlog cannot occupy all network slots while queued.
        with buffers.take(active, weight), self.network.take(active):
            yield


# Config and database are process-stable in production. Keeping the same
# coordinator for a DSN also preserves a failed journal latch between attempts.
# Disposable test databases have separate coordinators; no DSN is ever logged.
_registry_lock = threading.Lock()
_registry: dict[str, _Pipeline] = {}
_current: ContextVar[tuple[_Pipeline, dict[str, Any], Callable[[], None]] | None] = ContextVar(
    "upload_stage_capacity", default=None)


def _after_fork() -> None:
    global _registry_lock, _registry
    _registry_lock, _registry = threading.Lock(), {}
    _current.set(None)


os.register_at_fork(after_in_child=_after_fork)


def journal_status() -> dict[str, Any]:
    """Installation-local, bounded health signal; never expose DSNs or intent IDs."""
    from .. import database
    with _registry_lock:
        coordinator = _registry.get(database.settings.database_dsn)
    pending = len(coordinator.uncertain_journals()) if coordinator else 0
    return {"held": bool(pending), "uncertain_facts": pending}


def pipeline_for(worker: Any) -> _Pipeline:
    from .. import database
    with _registry_lock:
        dsn = database.settings.database_dsn
        if dsn not in _registry:
            _registry[dsn] = _Pipeline(worker._runtime_settings().operation_concurrency)
        return _registry[dsn]


@contextmanager
def bind(pipeline: _Pipeline, job: dict[str, Any], check: Callable[[], None]) -> Iterator[None]:
    token = _current.set((pipeline, job, check))
    try:
        yield
    finally:
        _current.reset(token)


class _CatalogWorker:
    """Give existing snapshot inventory/cleanup units the same DB admission.

    This is not a new protocol or a replacement for their target/keeper/claim
    fences. It only owns connection capacity, before checkout through commit.
    Exact deletion keeps its original locked, non-retried provider effect; it
    never waits for a new permit while holding those locks.
    """

    def __init__(self, worker: Any, job: dict[str, Any]):
        self.worker, self.job = worker, job

    def __getattr__(self, name: str) -> Any:
        return getattr(self.worker, name)

    @contextmanager
    def db(self):
        with catalog_capacity(self.job), owned_connection(self.worker.db()) as connection:
            yield connection


def catalog_worker(worker: Any, job: dict[str, Any]) -> Any:
    current = _current.get()
    return _CatalogWorker(worker, job) if current is not None and current[1]["id"] == job["id"] else worker


def preparation_capacity(job: dict[str, Any]):
    current = _current.get()
    if current is None or current[1]["id"] != job["id"]:
        return nullcontext()
    pipeline, _, check = current
    return pipeline.prepare.take(check, 2 * CHUNK_SIZE)


def catalog_capacity(job: dict[str, Any]):
    """Adapter hook, entered BEFORE opening an upload catalog connection.

    No ambient authority is transferred to SDK threads. Their receipt callback
    captures its journal/latch explicitly and bypasses this nonurgent capacity.
    Other Job types and standalone recovery/cleanup retain existing behavior;
    claimed-upload inventory/cleanup units use catalog_worker explicitly.
    """
    current = _current.get()
    if current is None or current[1]["id"] != job["id"]:
        return nullcontext()
    pipeline, _, check = current
    return pipeline.catalog_unit(job, check)
