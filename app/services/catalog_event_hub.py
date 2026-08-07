"""In-process fan-out for catalog revision notifications.

Durable revisions live in :mod:`app.services.catalog_events`.  This hub only
wakes authenticated SSE subscribers after a revision has been committed.  Events
for the same Vault are coalesced so a burst of filesystem changes yields one
browser invalidation carrying the newest revision and the union of domains.
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


DEFAULT_INVALIDATE_DOMAINS: tuple[str, ...] = (
    "files",
    "stats",
    "rename_candidates",
)


def normalize_invalidate_domains(value: Any | None) -> list[str]:
    """Return a stable, de-duplicated list of invalidation domains."""
    if value is None:
        domains: Iterable[str] = DEFAULT_INVALIDATE_DOMAINS
    elif isinstance(value, str):
        domains = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, Mapping):
        nested = value.get("invalidate")
        if nested is None:
            nested = value.get("domains")
        return normalize_invalidate_domains(nested)
    elif isinstance(value, Iterable):
        domains = [str(item).strip() for item in value if str(item).strip()]
    else:
        domains = DEFAULT_INVALIDATE_DOMAINS
    ordered: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        if domain in seen:
            continue
        seen.add(domain)
        ordered.append(domain)
    return ordered or list(DEFAULT_INVALIDATE_DOMAINS)


def signal_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Project a durable catalog event into the SSE invalidation contract."""
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    domains = normalize_invalidate_domains(payload)
    if not domains and event.get("domain"):
        domains = normalize_invalidate_domains(event.get("domain"))
    return {
        "vault_id": int(event["vault_id"]),
        "revision": int(event["revision"]),
        "domains": domains,
        "has_gap": bool(event.get("has_gap", False)),
    }


def coalesce_signals(*signals: Mapping[str, Any]) -> dict[str, Any] | None:
    """Merge one or more signals for the same Vault into the newest revision."""
    merged: dict[str, Any] | None = None
    for signal in signals:
        if signal is None:
            continue
        vault_id = int(signal["vault_id"])
        revision = int(signal["revision"])
        domains = normalize_invalidate_domains(signal.get("domains"))
        has_gap = bool(signal.get("has_gap", False))
        if merged is None:
            merged = {
                "vault_id": vault_id,
                "revision": revision,
                "domains": domains,
                "has_gap": has_gap,
            }
            continue
        if vault_id != int(merged["vault_id"]):
            raise ValueError("cannot coalesce catalog signals across Vaults")
        merged["revision"] = max(int(merged["revision"]), revision)
        merged["domains"] = normalize_invalidate_domains(
            list(merged["domains"]) + domains
        )
        merged["has_gap"] = bool(merged["has_gap"]) or has_gap
    return merged


@dataclass
class _Subscriber:
    """One live SSE consumer for a single Vault membership."""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    closed: bool = False

    def offer(self, signal: Mapping[str, Any]) -> None:
        if self.closed:
            return
        try:
            pending = self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pending = None
        merged = coalesce_signals(pending, signal) if pending is not None else dict(signal)
        assert merged is not None
        try:
            self.queue.put_nowait(merged)
        except asyncio.QueueFull:
            # Drop the older value; the newest revision is what reconnect needs.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(merged)
            except asyncio.QueueFull:
                return

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class CatalogEventHub:
    """Thread-safe registry of per-Vault SSE subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[int, list[_Subscriber]] = defaultdict(list)

    def subscribe(self, vault_id: int) -> _Subscriber:
        vault_id = int(vault_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("catalog event subscription requires a running loop") from exc
        subscriber = _Subscriber(loop=loop)
        with self._lock:
            self._subscribers[vault_id].append(subscriber)
        return subscriber

    def unsubscribe(self, vault_id: int, subscriber: _Subscriber) -> None:
        vault_id = int(vault_id)
        with self._lock:
            holders = self._subscribers.get(vault_id)
            if not holders:
                return
            self._subscribers[vault_id] = [
                item for item in holders if item is not subscriber
            ]
            if not self._subscribers[vault_id]:
                self._subscribers.pop(vault_id, None)
        subscriber.close()

    def publish(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Notify live subscribers after a durable revision is committed."""
        signal = signal_from_event(event)
        vault_id = int(signal["vault_id"])
        with self._lock:
            subscribers = list(self._subscribers.get(vault_id, []))
        for subscriber in subscribers:
            # offer() is safe for asyncio.Queue from the owning loop thread and
            # via call_soon_threadsafe from worker threads.
            if subscriber.loop.is_closed():
                continue
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is subscriber.loop:
                subscriber.offer(signal)
            else:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, signal)
        return signal

    def subscriber_count(self, vault_id: int | None = None) -> int:
        with self._lock:
            if vault_id is None:
                return sum(len(items) for items in self._subscribers.values())
            return len(self._subscribers.get(int(vault_id), ()))


catalog_event_hub = CatalogEventHub()


def publish_committed_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Module-level helper used by storage/scan workers after commit."""
    return catalog_event_hub.publish(event)
