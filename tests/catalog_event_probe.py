"""Catalog event reads and a whole-mutation publication that only tests need (#791).

Production appends events inside its own transactions and serves them through
the event stream; tests also publish a complete mutation in one call and read
the durable log back directly.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from app.database import transaction
from app.services import catalog_events
from app.services.catalog_events import CatalogEventStore

T = TypeVar("T")


@dataclass(frozen=True)
class MutationPublication(Generic[T]):
    """The canonical mutation result and its committed-on-success event."""

    result: T
    event: dict[str, Any]


def mutate_and_publish(
    store: CatalogEventStore,
    vault_id: int,
    mutation: Callable[[Any], T],
    *,
    domain: str | None = None,
    event_type: str | None = None,
    scope: str | None = None,
    payload: Mapping[str, Any] | None = None,
    payload_json: str | None = None,
    created_at: str | None = None,
) -> MutationPublication[T]:
    """Run canonical mutation and revision publication atomically.

    The Vault lock is acquired before invoking ``mutation``.  If either the
    callback or event insert fails, a transaction owned by this method is
    rolled back; an outer transaction remains the caller's responsibility.
    """
    if not callable(mutation):
        raise TypeError("mutation must be callable")
    vault_id = catalog_events._positive_or_zero(vault_id, name="vault_id")
    if vault_id == 0:
        raise ValueError("vault_id must be positive")
    event_domain, event_scope, event_payload, stamp = catalog_events._prepare_event(
        domain=domain,
        event_type=event_type,
        scope=scope,
        payload=payload,
        payload_json=payload_json,
        created_at=created_at,
    )
    with transaction(store.connection):
        store._lock_vault(vault_id)
        result = mutation(store.connection)
        event = store._append_locked(
            vault_id=vault_id,
            domain=event_domain,
            scope=event_scope,
            payload_json=event_payload,
            created_at=stamp,
        )
    return MutationPublication(result=result, event=event)


def current_revision(store: CatalogEventStore, vault_id: int) -> int:
    vault_id = catalog_events._positive_or_zero(vault_id, name="vault_id")
    if vault_id == 0:
        raise ValueError("vault_id must be positive")
    row = store.connection.execute(
        "SELECT revision FROM vault_catalog_revisions WHERE vault_id=%s",
        (vault_id,),
    ).fetchone()
    return int(row["revision"]) if row else 0


def read_events(
    store: CatalogEventStore,
    *,
    vault_id: int,
    after_revision: int = 0,
    limit: int = catalog_events.MAX_EVENT_PAGE_SIZE,
) -> dict[str, Any]:
    vault_id = catalog_events._positive_or_zero(vault_id, name="vault_id")
    after_revision = catalog_events._positive_or_zero(after_revision, name="after_revision")
    if vault_id == 0:
        raise ValueError("vault_id must be positive")
    if limit <= 0:
        raise ValueError("limit must be positive")
    limit = min(int(limit), catalog_events.MAX_EVENT_PAGE_SIZE)
    state = store.connection.execute(
        """
        SELECT revision, retained_from_revision
        FROM vault_catalog_revisions
        WHERE vault_id=%s
        """,
        (vault_id,),
    ).fetchone()
    current = int(state["revision"]) if state else 0
    retained_from = int(state["retained_from_revision"]) if state else 1
    rows = store.connection.execute(
        """
        SELECT id, vault_id, revision, domain, scope, payload, created_at
        FROM catalog_events
        WHERE vault_id=%s AND revision>%s
        ORDER BY revision
        LIMIT %s
        """,
        (vault_id, after_revision, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    visible = rows[:limit]
    expected = max(after_revision + 1, retained_from)
    has_gap = after_revision < retained_from - 1
    if visible and int(visible[0]["revision"]) > expected:
        has_gap = True
    if not visible and after_revision < current:
        # A missing event inside the retained interval is corruption or an
        # interrupted rebuild, not a normal empty page.  Surface it as a
        # resync gap rather than allowing a reconnect to silently advance.
        has_gap = True
    return {
        "vault_id": vault_id,
        "current_revision": current,
        "retained_from_revision": retained_from,
        "has_gap": has_gap,
        "retention_gap": has_gap,
        "has_more": has_more,
        "events": [catalog_events._event_from_row(row) for row in visible],
    }
