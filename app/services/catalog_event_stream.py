"""Authenticated catalog invalidation stream (SSE) seam.

Architecture and cost
=====================

The durable ``catalog_events`` / ``vault_catalog_revisions`` journal is the
multi-process source of truth.  The in-process hub is **wake-up only**: every
advance of the client high-water mark is computed by re-reading the journal
from ``last_seen``, never by trusting a hub payload alone.

Each open stream performs one short-lived tick on a bounded cadence
(``DURABLE_POLL_SECONDS``, default 2s).  A hub notification only short-circuits
the wait; the tick still runs the same single-connection snapshot.

**Per tick, one DB connection, three short statements (no long transaction):**

1. Session + user + membership authorization (single JOIN).
2. Vault catalog high-water / retention markers.
3. At most ``MAX_CATCHUP_EVENTS + 1`` journal rows after ``last_seen``.

If the backlog exceeds the bound (``has_more``), the tick emits one coalesced
``has_gap`` / invalidate-all signal at the high-water revision instead of
paging the whole journal.  Connections are opened and closed per tick; nothing
is held across awaits.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ..database import db
from .catalog_event_hub import (
    catalog_event_hub,
    coalesce_signals,
    normalize_invalidate_domains,
    signal_from_event,
)
from .catalog_events import MAX_EVENT_PAGE_SIZE


# Wake / multi-process observation cadence.  One consolidated snapshot per tick.
DURABLE_POLL_SECONDS = 2.0

# Hard cap on journal rows materialized per catch-up.  Larger backlogs become a
# single has_gap invalidate-all at the high-water revision.
MAX_CATCHUP_EVENTS = min(64, MAX_EVENT_PAGE_SIZE)

StreamErrorCode = Literal[
    "session_revoked",
    "session_expired",
    "session_version_mismatch",
    "user_disabled",
    "vault_access_revoked",
    "vault_switched",
]


@dataclass(frozen=True)
class StreamTickSnapshot:
    """Result of one authorization + journal observation tick."""

    ok: bool
    error: StreamErrorCode | None = None
    session_vault_id: int | None = None
    signal: dict[str, Any] | None = None
    # Observability for tests/operators — not part of the wire contract.
    statement_count: int = 0
    events_read: int = 0
    backlog_truncated: bool = False


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


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _invalidate_all_signal(*, vault_id: int, revision: int) -> dict[str, Any]:
    return {
        "vault_id": vault_id,
        "revision": int(revision),
        "domains": normalize_invalidate_domains(None),
        "has_gap": True,
    }


def coalesced_catchup_from_connection(
    connection: Any,
    *,
    vault_id: int,
    after_revision: int,
    max_events: int = MAX_CATCHUP_EVENTS,
    statement_counter: list[int] | None = None,
) -> tuple[dict[str, Any], int, bool]:
    """Bounded journal catch-up on an already-open connection.

    Returns ``(signal, events_read, backlog_truncated)``.
    Never loops pages: one high-water read + one bounded event page.
    """
    max_events = max(1, min(int(max_events), MAX_EVENT_PAGE_SIZE))

    def _count() -> None:
        if statement_counter is not None:
            statement_counter[0] += 1

    _count()
    state = connection.execute(
        """
        SELECT revision, retained_from_revision
        FROM vault_catalog_revisions
        WHERE vault_id=%s
        """,
        (vault_id,),
    ).fetchone()
    current = int(state["revision"]) if state else 0
    retained_from = int(state["retained_from_revision"]) if state else 1

    if after_revision >= current and after_revision >= retained_from - 1:
        return (
            {
                "vault_id": vault_id,
                "revision": current,
                "domains": [],
                "has_gap": False,
            },
            0,
            False,
        )

    # Retention gap or cursor behind retained history → full invalidate.
    if after_revision < retained_from - 1:
        return _invalidate_all_signal(vault_id=vault_id, revision=current), 0, True

    _count()
    rows = connection.execute(
        """
        SELECT id, vault_id, revision, domain, scope, payload_json, created_at
        FROM catalog_events
        WHERE vault_id=%s AND revision>%s
        ORDER BY revision
        LIMIT %s
        """,
        (vault_id, after_revision, max_events + 1),
    ).fetchall()
    has_more = len(rows) > max_events
    visible = rows[:max_events]
    events_read = len(visible)

    if not visible:
        if after_revision < current:
            # Missing retained events → treat as gap, do not silently advance.
            return (
                _invalidate_all_signal(vault_id=vault_id, revision=current),
                0,
                True,
            )
        return (
            {
                "vault_id": vault_id,
                "revision": current,
                "domains": [],
                "has_gap": False,
            },
            0,
            False,
        )

    expected = max(after_revision + 1, retained_from)
    if int(visible[0]["revision"]) > expected:
        return (
            _invalidate_all_signal(vault_id=vault_id, revision=current),
            events_read,
            True,
        )

    if has_more:
        # Bound exceeded: do not page.  Client converges via invalidate-all.
        return (
            _invalidate_all_signal(vault_id=vault_id, revision=current),
            events_read,
            True,
        )

    decoded: list[dict[str, Any]] = []
    for row in visible:
        payload_raw = row["payload_json"]
        if isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            try:
                payload = json.loads(payload_raw or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
        decoded.append(
            signal_from_event(
                {
                    "vault_id": row["vault_id"],
                    "revision": int(row["revision"]),
                    "domain": row["domain"],
                    "scope": row["scope"],
                    "payload": payload,
                    "created_at": row["created_at"],
                    "has_gap": False,
                }
            )
        )
    merged = coalesce_signals(*decoded)
    if merged is None:
        return (
            {
                "vault_id": vault_id,
                "revision": current,
                "domains": [],
                "has_gap": False,
            },
            events_read,
            False,
        )
    # Fully consumed bounded page: high-water is the newest visible revision.
    merged["has_gap"] = bool(merged.get("has_gap"))
    return merged, events_read, False


def stream_tick_snapshot(
    *,
    session_id: str,
    user_id: int,
    vault_id: int,
    after_revision: int,
    max_events: int = MAX_CATCHUP_EVENTS,
    now: datetime | None = None,
) -> StreamTickSnapshot:
    """Authorize the stream and observe the journal on **one** short connection.

    Cost: one connection, three statements (auth JOIN + high-water + bounded
    event page).  No transaction is held across awaits — the caller closes the
    connection before sleeping or yielding.
    """
    vault_id = int(vault_id)
    user_id = int(user_id)
    after_revision = max(0, int(after_revision))
    stamp = now or datetime.now(timezone.utc)
    statements = [0]

    with db() as connection:
        statements[0] += 1
        row = connection.execute(
            """
            SELECT s.vault_id AS session_vault_id,
                   s.revoked_at AS revoked_at,
                   s.idle_expires_at AS idle_expires_at,
                   s.absolute_expires_at AS absolute_expires_at,
                   s.session_version AS session_session_version,
                   u.session_version AS user_session_version,
                   u.active AS user_active,
                   CASE
                     WHEN vm.user_id IS NOT NULL AND v.id IS NOT NULL THEN 1
                     ELSE 0
                   END AS has_membership
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN vault_members vm
              ON vm.user_id = s.user_id
             AND vm.vault_id = %s
            LEFT JOIN vaults v
              ON v.id = vm.vault_id
             AND v.enabled = TRUE
             AND v.decommission_state = 'active'
            WHERE s.id = %s AND s.user_id = %s
            """,
            (vault_id, session_id, user_id),
        ).fetchone()

        if row is None:
            return StreamTickSnapshot(
                ok=False,
                error="session_revoked",
                statement_count=statements[0],
            )
        if row["revoked_at"]:
            return StreamTickSnapshot(
                ok=False,
                error="session_revoked",
                statement_count=statements[0],
            )
        if not row["user_active"]:
            return StreamTickSnapshot(
                ok=False,
                error="user_disabled",
                statement_count=statements[0],
            )
        if row["session_session_version"] != row["user_session_version"]:
            return StreamTickSnapshot(
                ok=False,
                error="session_version_mismatch",
                statement_count=statements[0],
            )

        absolute = _parse_iso(row["absolute_expires_at"])
        idle = _parse_iso(row["idle_expires_at"])
        if absolute is None or idle is None or stamp >= absolute or stamp >= idle:
            return StreamTickSnapshot(
                ok=False,
                error="session_expired",
                statement_count=statements[0],
            )

        session_vault_id = (
            int(row["session_vault_id"])
            if row["session_vault_id"] is not None
            else None
        )
        if session_vault_id is None or session_vault_id != vault_id:
            return StreamTickSnapshot(
                ok=False,
                error="vault_switched",
                session_vault_id=session_vault_id,
                statement_count=statements[0],
            )

        # Membership against the subscribed vault (fail closed).
        if not int(row["has_membership"] or 0):
            return StreamTickSnapshot(
                ok=False,
                error="vault_access_revoked",
                session_vault_id=session_vault_id,
                statement_count=statements[0],
            )

        signal, events_read, truncated = coalesced_catchup_from_connection(
            connection,
            vault_id=vault_id,
            after_revision=after_revision,
            max_events=max_events,
            statement_counter=statements,
        )
        return StreamTickSnapshot(
            ok=True,
            session_vault_id=session_vault_id,
            signal=signal,
            statement_count=statements[0],
            events_read=events_read,
            backlog_truncated=truncated,
        )


def coalesced_catchup_signal(
    *,
    vault_id: int,
    after_revision: int,
    max_events: int = MAX_CATCHUP_EVENTS,
) -> dict[str, Any]:
    """Public catch-up helper (hello / one-shot revision endpoint)."""
    with db() as connection:
        signal, _events_read, _truncated = coalesced_catchup_from_connection(
            connection,
            vault_id=int(vault_id),
            after_revision=max(0, int(after_revision)),
            max_events=max_events,
        )
    return signal


# Backward-compatible names used by older tests/callers.
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
    """Return live session vault scope, or None when invalid (compat helper)."""
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
    if row is None or row["revoked_at"] or not row["user_active"]:
        return None
    if row["session_session_version"] != row["user_session_version"]:
        return None
    now = datetime.now(timezone.utc)
    absolute = _parse_iso(row["absolute_expires_at"])
    idle = _parse_iso(row["idle_expires_at"])
    if absolute is None or idle is None or now >= absolute or now >= idle:
        return None
    vault_id = row["vault_id"]
    return {"vault_id": int(vault_id) if vault_id is not None else None}


DisconnectProbe = Callable[[], Awaitable[bool]]


def _error_frame(code: StreamErrorCode, vault_id: int, **extra: Any) -> str:
    payload: dict[str, Any] = {"error": code, "vault_id": vault_id}
    payload.update(extra)
    return catalog_event_sse_frame("error", payload)


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
    max_events: int = MAX_CATCHUP_EVENTS,
) -> AsyncIterator[str]:
    """Yield SSE frames for one authorized catalog subscription.

    Hub notifications only wake the wait loop.  Advancement always comes from
    ``stream_tick_snapshot`` journal observation starting at ``last_seen``.
    """
    vault_id = int(vault_id)
    user_id = int(user_id)
    subscriber = None
    if subscribe and use_hub:
        subscriber = catalog_event_hub.subscribe(vault_id)
    try:
        initial = await asyncio.to_thread(
            stream_tick_snapshot,
            session_id=session_id,
            user_id=user_id,
            vault_id=vault_id,
            after_revision=resume_after,
            max_events=max_events,
        )
        if not initial.ok:
            code = initial.error or "session_revoked"
            yield _error_frame(
                code,
                vault_id,
                current_vault_id=initial.session_vault_id,
            )
            return

        hello_revision = int((initial.signal or {}).get("revision") or 0)
        yield catalog_event_sse_frame(
            "hello",
            {"vault_id": vault_id, "revision": hello_revision},
            event_id=str(hello_revision) if hello_revision > 0 else None,
        )

        last_seen = resume_after
        signal = initial.signal or {
            "vault_id": vault_id,
            "revision": hello_revision,
            "domains": [],
            "has_gap": False,
        }
        if int(signal["revision"]) > resume_after or signal.get("has_gap"):
            yield catalog_event_sse_frame(
                "catalog",
                {
                    "vault_id": int(signal["vault_id"]),
                    "revision": int(signal["revision"]),
                    "domains": list(signal.get("domains") or []),
                    "has_gap": bool(signal.get("has_gap")),
                },
                event_id=str(int(signal["revision"])),
            )
            last_seen = max(last_seen, int(signal["revision"]))

        if not subscribe:
            return

        while True:
            if is_disconnected is not None and await is_disconnected():
                break

            # Hub is wake-up only: wait for notification or cadence timeout.
            if subscriber is not None:
                get_task = asyncio.create_task(subscriber.queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        {get_task}, timeout=durable_poll_seconds
                    )
                    if done:
                        # Drain/coalesce wake-ups; ignore payload content.
                        while True:
                            try:
                                subscriber.queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
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

            if is_disconnected is not None and await is_disconnected():
                break

            tick = await asyncio.to_thread(
                stream_tick_snapshot,
                session_id=session_id,
                user_id=user_id,
                vault_id=vault_id,
                after_revision=last_seen,
                max_events=max_events,
            )
            if not tick.ok:
                code = tick.error or "session_revoked"
                yield _error_frame(
                    code,
                    vault_id,
                    current_vault_id=tick.session_vault_id,
                )
                break

            signal = tick.signal or {
                "vault_id": vault_id,
                "revision": last_seen,
                "domains": [],
                "has_gap": False,
            }
            revision = int(signal["revision"])
            if revision <= last_seen and not signal.get("has_gap"):
                yield ": keepalive\n\n"
                continue

            # Dedupe: never emit a non-gap frame at or behind last_seen.
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
