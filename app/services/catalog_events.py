"""Durable catalog revisions and events.

This module is intentionally a persistence seam, not a read-path integration.
Callers own the canonical catalog mutation and pass the same database
connection to :class:`CatalogEventStore` so the mutation, revision allocation,
and event insert commit or roll back together.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from ..database import transaction


MAX_EVENT_DOMAIN_LENGTH = 64
MAX_EVENT_SCOPE_LENGTH = 512
MAX_EVENT_PAYLOAD_BYTES = 4096
MAX_EVENT_PAGE_SIZE = 100

_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")

T = TypeVar("T")

@dataclass(frozen=True)
class MutationPublication(Generic[T]):
    """The canonical mutation result and its committed-on-success event."""

    result: T
    event: dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(
    value: Any,
    *,
    name: str,
    maximum: int,
    allow_empty: bool = True,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"{name} must be text")
    if not allow_empty and not text.strip():
        raise ValueError(f"{name} must not be empty")
    if "\x00" in text:
        raise ValueError(f"{name} contains a NUL byte")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds the {maximum}-character bound")
    return text


def _positive_or_zero(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _json_object(value: Mapping[str, Any] | None, *, name: str, maximum: int) -> str:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds its bounded payload size")
    return encoded


def _decode_object(value: str | None, *, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Persisted {name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"Persisted {name} is not a JSON object")
    return decoded


def _domain(value: str | None, event_type: str | None) -> str:
    if value is None:
        value = event_type
    elif event_type is not None and value != event_type:
        raise ValueError("domain and event_type must agree when both are supplied")
    text = _bounded_text(
        value,
        name="domain",
        maximum=MAX_EVENT_DOMAIN_LENGTH,
        allow_empty=False,
    )
    if _DOMAIN_RE.fullmatch(text) is None:
        raise ValueError("domain contains unsupported characters")
    return text


def _prepare_event(
    *,
    domain: str | None,
    event_type: str | None,
    scope: str | None,
    payload: Mapping[str, Any] | None,
    payload_json: str | None,
    created_at: str | None,
) -> tuple[str, str, str, str]:
    event_domain = _domain(domain, event_type)
    event_scope = _bounded_text(
        scope,
        name="scope",
        maximum=MAX_EVENT_SCOPE_LENGTH,
    )
    if payload_json is not None:
        if payload is not None:
            raise ValueError("payload and payload_json are mutually exclusive")
        try:
            decoded = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("payload_json must contain valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("payload_json must contain a JSON object")
        event_payload = _json_object(
            decoded,
            name="catalog event payload",
            maximum=MAX_EVENT_PAYLOAD_BYTES,
        )
    else:
        event_payload = _json_object(
            payload,
            name="catalog event payload",
            maximum=MAX_EVENT_PAYLOAD_BYTES,
        )
    stamp = _bounded_text(
        created_at or _now(),
        name="created_at",
        maximum=128,
        allow_empty=False,
    )
    return event_domain, event_scope, event_payload, stamp


def _event_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "vault_id": row["vault_id"],
        "revision": int(row["revision"]),
        "domain": row["domain"],
        "scope": row["scope"],
        "payload": _decode_object(row.get("payload_json"), name="catalog event payload"),
        "created_at": row["created_at"],
    }


class CatalogEventStore:
    """Allocate per-Vault revisions and append bounded catalog events.

    The interface never commits an ordinary ``append_event`` call.  This lets a
    scanner or worker put its canonical writes and the event in one caller-owned
    transaction.  ``mutate_and_publish`` is the convenience form for a complete
    mutation callback and owns a transaction only when the caller has not
    already opened one.
    """

    def __init__(self, connection: Any):
        self.connection = connection

    def _lock_vault(self, vault_id: int) -> None:
        # The existing Vault row is the cross-backend serialization point.  An
        # UPDATE that writes the same value still takes a row lock on PostgreSQL
        # and the database write lock on SQLite, without changing canonical data.
        result = self.connection.execute(
            "UPDATE vaults SET name=name WHERE id=%s",
            (vault_id,),
        )
        if result.rowcount != 1:
            raise LookupError(f"Vault {vault_id} does not exist")

    def _ensure_revision_row(self, vault_id: int, *, at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO vault_catalog_revisions(
                vault_id, revision, retained_from_revision, updated_at
            ) VALUES (%s, 0, 1, %s)
            ON CONFLICT(vault_id) DO NOTHING
            """,
            (vault_id, at),
        )

    def _append_locked(
        self,
        *,
        vault_id: int,
        domain: str,
        scope: str,
        payload_json: str,
        created_at: str,
    ) -> dict[str, Any]:
        self._ensure_revision_row(vault_id, at=created_at)
        self.connection.execute(
            """
            UPDATE vault_catalog_revisions
            SET revision=revision + 1, updated_at=%s
            WHERE vault_id=%s
            """,
            (created_at, vault_id),
        )
        state = self.connection.execute(
            """
            SELECT revision, retained_from_revision
            FROM vault_catalog_revisions
            WHERE vault_id=%s
            """,
            (vault_id,),
        ).fetchone()
        if state is None:
            raise RuntimeError("catalog revision row disappeared during publication")
        revision = int(state["revision"])
        if revision <= 0:
            raise RuntimeError("catalog revision did not advance")
        self.connection.execute(
            """
            INSERT INTO catalog_events(
                vault_id, revision, domain, scope, payload_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (vault_id, revision, domain, scope, payload_json, created_at),
        )
        row = self.connection.execute(
            """
            SELECT id, vault_id, revision, domain, scope, payload_json, created_at
            FROM catalog_events
            WHERE vault_id=%s AND revision=%s
            """,
            (vault_id, revision),
        ).fetchone()
        if row is None:
            raise RuntimeError("catalog event disappeared during publication")
        return _event_from_row(row)

    def append_event(
        self,
        *,
        vault_id: int,
        domain: str | None = None,
        event_type: str | None = None,
        scope: str | None = None,
        payload: Mapping[str, Any] | None = None,
        payload_json: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Append one event in the caller's transaction and return its revision."""
        vault_id = _positive_or_zero(vault_id, name="vault_id")
        if vault_id == 0:
            raise ValueError("vault_id must be positive")
        event_domain, event_scope, event_payload, stamp = _prepare_event(
            domain=domain,
            event_type=event_type,
            scope=scope,
            payload=payload,
            payload_json=payload_json,
            created_at=created_at,
        )
        self._lock_vault(vault_id)
        return self._append_locked(
            vault_id=vault_id,
            domain=event_domain,
            scope=event_scope,
            payload_json=event_payload,
            created_at=stamp,
        )

    # These names keep the seam easy to discover for callers that think in
    # terms of publishing rather than appending.
    append = append_event
    publish = append_event

    def mutate_and_publish(
        self,
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
        vault_id = _positive_or_zero(vault_id, name="vault_id")
        if vault_id == 0:
            raise ValueError("vault_id must be positive")
        event_domain, event_scope, event_payload, stamp = _prepare_event(
            domain=domain,
            event_type=event_type,
            scope=scope,
            payload=payload,
            payload_json=payload_json,
            created_at=created_at,
        )
        with transaction(self.connection, immediate=True):
            self._lock_vault(vault_id)
            result = mutation(self.connection)
            event = self._append_locked(
                vault_id=vault_id,
                domain=event_domain,
                scope=event_scope,
                payload_json=event_payload,
                created_at=stamp,
            )
        return MutationPublication(result=result, event=event)

    def current_revision(self, vault_id: int) -> int:
        vault_id = _positive_or_zero(vault_id, name="vault_id")
        if vault_id == 0:
            raise ValueError("vault_id must be positive")
        row = self.connection.execute(
            "SELECT revision FROM vault_catalog_revisions WHERE vault_id=%s",
            (vault_id,),
        ).fetchone()
        return int(row["revision"]) if row else 0

    get_current_revision = current_revision

    def read_events(
        self,
        *,
        vault_id: int,
        after_revision: int = 0,
        limit: int = MAX_EVENT_PAGE_SIZE,
    ) -> dict[str, Any]:
        vault_id = _positive_or_zero(vault_id, name="vault_id")
        after_revision = _positive_or_zero(after_revision, name="after_revision")
        if vault_id == 0:
            raise ValueError("vault_id must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")
        limit = min(int(limit), MAX_EVENT_PAGE_SIZE)
        state = self.connection.execute(
            """
            SELECT revision, retained_from_revision
            FROM vault_catalog_revisions
            WHERE vault_id=%s
            """,
            (vault_id,),
        ).fetchone()
        current = int(state["revision"]) if state else 0
        retained_from = int(state["retained_from_revision"]) if state else 1
        rows = self.connection.execute(
            """
            SELECT id, vault_id, revision, domain, scope, payload_json, created_at
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
            "events": [_event_from_row(row) for row in visible],
        }

    read_since = read_events

    def prune_events(
        self,
        *,
        vault_id: int,
        retain_from_revision: int,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Delete old events while preserving a durable retention-gap marker."""
        vault_id = _positive_or_zero(vault_id, name="vault_id")
        retain_from_revision = _positive_or_zero(
            retain_from_revision,
            name="retain_from_revision",
        )
        if vault_id == 0:
            raise ValueError("vault_id must be positive")
        if retain_from_revision == 0:
            raise ValueError("retain_from_revision must be positive")
        stamp = _bounded_text(
            updated_at or _now(),
            name="updated_at",
            maximum=128,
            allow_empty=False,
        )
        self._lock_vault(vault_id)
        state = self.connection.execute(
            """
            SELECT revision, retained_from_revision
            FROM vault_catalog_revisions
            WHERE vault_id=%s
            """,
            (vault_id,),
        ).fetchone()
        if state is None:
            if retain_from_revision != 1:
                raise ValueError("cannot retain beyond an uninitialized revision")
            return {
                "vault_id": vault_id,
                "deleted": 0,
                "revision": 0,
                "retained_from_revision": 1,
            }
        current = int(state["revision"])
        if retain_from_revision > current + 1:
            raise ValueError("retain_from_revision cannot skip future revisions")
        deleted = self.connection.execute(
            "DELETE FROM catalog_events WHERE vault_id=%s AND revision<%s",
            (vault_id, retain_from_revision),
        ).rowcount
        retained_from = max(
            int(state["retained_from_revision"]), retain_from_revision
        )
        self.connection.execute(
            """
            UPDATE vault_catalog_revisions
            SET retained_from_revision=%s, updated_at=%s
            WHERE vault_id=%s
            """,
            (retained_from, stamp, vault_id),
        )
        return {
            "vault_id": vault_id,
            "deleted": max(int(deleted), 0),
            "revision": current,
            "retained_from_revision": retained_from,
        }

    prune_before = prune_events


def append_catalog_event(connection: Any, **kwargs: Any) -> dict[str, Any]:
    return CatalogEventStore(connection).append_event(**kwargs)


def publish_catalog_mutation(
    connection: Any,
    vault_id: int,
    mutation: Callable[[Any], T],
    **kwargs: Any,
) -> MutationPublication[T]:
    return CatalogEventStore(connection).mutate_and_publish(vault_id, mutation, **kwargs)


def record_catalog_revision(
    connection: Any,
    *,
    vault_id: int,
    reason: str,
    invalidate: Iterable[str] | None = None,
    scope: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Append one broad catalog invalidation event in the caller's transaction."""
    from .catalog_event_hub import DEFAULT_INVALIDATE_DOMAINS, normalize_invalidate_domains
    from .directory_aggregates import flush_directory_aggregates

    # Keep durable directory aggregates inside the same transaction as the
    # catalog mutation + revision so a rolled-back burst never leaves rollups.
    flush_directory_aggregates(connection, vault_id=int(vault_id))

    domains = normalize_invalidate_domains(
        invalidate if invalidate is not None else DEFAULT_INVALIDATE_DOMAINS
    )
    payload: dict[str, Any] = {
        "invalidate": domains,
        "reason": str(reason or "catalog"),
    }
    if extra_payload:
        for key, value in dict(extra_payload).items():
            if key in {"invalidate", "reason"}:
                continue
            payload[key] = value
    return CatalogEventStore(connection).append_event(
        vault_id=vault_id,
        domain="catalog",
        scope=scope or "",
        payload=payload,
        created_at=created_at,
    )
