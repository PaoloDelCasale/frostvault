"""Authenticated catalog invalidation stream (SSE) seam.

The durable ``catalog_events`` journal is the multi-process source of truth.
In-process hub fan-out is an optional fast path; every open stream also observes
the journal on a bounded cadence so revisions committed by another worker
reach live subscribers without client reconnect or idle polling.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ..database import db
from .catalog_event_hub import (
    catalog_event_hub,
    coalesce_signals,
    normalize_invalidate_domains,
    signal_from_event,
)
from .catalog_events import CatalogEventStore


DURABLE_POLL_SECONDS = 1.0


def catalog_event_sse_frame(
    event_name: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'), sort_keys=True)}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def user_can_access_vault(user_id: int, vault_id: int) -> bool:
    with db() as connection:
        row = connection.execute(
            """
            SELECT 1 AS ok
            FROM vault_members vm
            JOIN vaults v ON v.id=vm.vault_id
            WHERE vm.user_id=%s AND vm.vault_id=%s
              AND v.enabled=TRUE AND v.decommission_state='active'
            LIMIT 1
            """,
            (user_id, vault_id),
        ).fetchone()
    return bool(row)


def session_stream_state(session_id: str) -> dict[str, Any] | None:
    """Return live session vault scope, or None when revoked/expired/invalid.

    Read-only: does not slide idle expiry. Long-lived streams must still end when
    the Session is revoked or its absolute/idle deadlines elapse.
    """
    with db() as connection:
        row = connection.execute(
            """
            SELECT s.vault_id AS vault_id,
                   s.revoked_at AS revoked_at,
                   s.idle_expires_at AS idle_expires_at,
                   s.absolute_expires_at AS absolute_expires_at,
                   s.session_version AS session_session_version,
                   u.session_version AS user_session_version,
                   u.active AS user_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id=%s
            """,
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    if row["revoked_at"] or not row["user_active"]:
        return None
    if row["session_session_version"] != row["user_session_version"]:
        return None
    now = datetime.now(timezone.utc)

    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(str(value))

    try:
        if now >= _parse(row["absolute_expires_at"]) or now >= _parse(
            row["idle_expires_at"]
        ):
            return None
    except (TypeError, ValueError):
        return None
    vault_id = row["vault_id"]
    return {
        "vault_id": int(vault_id) if vault_id is not None else None,
    }


def coalesced_catchup_signal(
    *,
    vault_id: int,
    after_revision: int,
) -> dict[str, Any]:
    """Build one catch-up signal from durable events, never one frame per row."""
    with db() as connection:
        page = CatalogEventStore(connection).read_events(
            vault_id=vault_id,
            after_revision=after_revision,
            limit=100,
        )
        current = int(page["current_revision"])
        if page["has_gap"]:
            return {
                "vault_id": vault_id,
                "revision": current,
                "domains": normalize_invalidate_domains(None),
                "has_gap": True,
            }
        signals = [signal_from_event(event) for event in page["events"]]
        while page["has_more"]:
            last_revision = int(page["events"][-1]["revision"])
            page = CatalogEventStore(connection).read_events(
                vault_id=vault_id,
                after_revision=last_revision,
                limit=100,
            )
            signals.extend(signal_from_event(event) for event in page["events"])
            if page["has_gap"]:
                return {
                    "vault_id": vault_id,
                    "revision": int(page["current_revision"]),
                    "domains": normalize_invalidate_domains(None),
                    "has_gap": True,
                }
    merged = coalesce_signals(*signals)
    if merged is None:
        return {
            "vault_id": vault_id,
            "revision": current,
            "domains": [],
            "has_gap": False,
        }
    return merged


DisconnectProbe = Callable[[], Awaitable[bool]]


async def iter_catalog_event_sse(
    *,
    vault_id: int,
    user_id: int,
    session_id: str,
    resume_after: int = 0,
    subscribe: bool = True,
    is_disconnected: DisconnectProbe | None = None,
    durable_poll_seconds: float = DURABLE_POLL_SECONDS,
    use_hub: bool = True,
) -> AsyncIterator[str]:
    """Yield SSE frames for one authorized catalog subscription.

    ``use_hub=False`` forces durable-journal observation only (multi-process and
    test seam). When the hub is enabled it remains a fast path, but the journal
    is still observed on every wait timeout.
    """
    subscriber = None
    if subscribe and use_hub:
        subscriber = catalog_event_hub.subscribe(vault_id)
    try:
        hello = await asyncio.to_thread(
            coalesced_catchup_signal,
            vault_id=vault_id,
            after_revision=resume_after,
        )
        yield catalog_event_sse_frame(
            "hello",
            {
                "vault_id": vault_id,
                "revision": int(hello["revision"]),
            },
            event_id=str(int(hello["revision"])) if int(hello["revision"]) > 0 else None,
        )
        if int(hello["revision"]) > resume_after or hello.get("has_gap"):
            yield catalog_event_sse_frame(
                "catalog",
                {
                    "vault_id": int(hello["vault_id"]),
                    "revision": int(hello["revision"]),
                    "domains": list(hello.get("domains") or []),
                    "has_gap": bool(hello.get("has_gap")),
                },
                event_id=str(int(hello["revision"])),
            )
            last_seen = int(hello["revision"])
        else:
            last_seen = resume_after

        if not subscribe:
            return

        while True:
            if is_disconnected is not None and await is_disconnected():
                break

            if not await asyncio.to_thread(user_can_access_vault, user_id, vault_id):
                yield catalog_event_sse_frame(
                    "error",
                    {"error": "vault_access_revoked", "vault_id": vault_id},
                )
                break

            session_state = await asyncio.to_thread(session_stream_state, session_id)
            if session_state is None:
                yield catalog_event_sse_frame(
                    "error",
                    {"error": "session_revoked", "vault_id": vault_id},
                )
                break
            current_session_vault = session_state.get("vault_id")
            if current_session_vault is None or int(current_session_vault) != vault_id:
                yield catalog_event_sse_frame(
                    "error",
                    {
                        "error": "vault_switched",
                        "vault_id": vault_id,
                        "current_vault_id": current_session_vault,
                    },
                )
                break

            signal: dict[str, Any] | None = None
            if subscriber is not None:
                get_task = asyncio.create_task(subscriber.queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        {get_task}, timeout=durable_poll_seconds
                    )
                    if done:
                        signal = get_task.result()
                    else:
                        get_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await get_task
                except Exception:
                    get_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_task
                    raise
            else:
                await asyncio.sleep(durable_poll_seconds)

            if signal is None:
                # Multi-process path: observe durable journal even without hub.
                durable = await asyncio.to_thread(
                    coalesced_catchup_signal,
                    vault_id=vault_id,
                    after_revision=last_seen,
                )
                if int(durable["revision"]) > last_seen or durable.get("has_gap"):
                    signal = durable
                else:
                    yield ": keepalive\n\n"
                    continue

            if signal is None:
                break
            revision = int(signal["revision"])
            if revision <= last_seen and not signal.get("has_gap"):
                continue
            last_seen = max(last_seen, revision)
            yield catalog_event_sse_frame(
                "catalog",
                {
                    "vault_id": int(signal["vault_id"]),
                    "revision": revision,
                    "domains": list(signal.get("domains") or []),
                    "has_gap": bool(signal.get("has_gap")),
                },
                event_id=str(revision),
            )
    finally:
        if subscriber is not None:
            catalog_event_hub.unsubscribe(vault_id, subscriber)
