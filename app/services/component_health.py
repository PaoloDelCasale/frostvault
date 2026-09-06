"""Persisted per-component scan/audit health (issue #303)."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .notifications import now_iso


COMPONENTS = ("source", "cloud", "policy", "audit")
AUDIT_REPORT_KEYS = (
    "catalog_versions",
    "cloud_versions",
    "missing_in_cloud",
    "missing_in_catalog",
    "storage_class_drift",
    "policy_tag_drift",
    "missing_delete_markers",
    "healthy",
    "command_ok",
    "incidents_opened",
    "incidents_resolved",
)


def record_component_health(
    connection: Any,
    *,
    vault_id: int,
    component: str,
    ok: bool,
    error: str | None = None,
    verified: bool = False,
    extra: Mapping[str, Any] | None = None,
    at: str | None = None,
) -> None:
    """Upsert one component's last success, error, and verification time."""
    if component not in COMPONENTS:
        raise ValueError(f"invalid health component: {component}")
    stamp = at or now_iso()
    extra_json = json.dumps(dict(extra), sort_keys=True) if extra else None
    existing = connection.execute(
        """
        SELECT last_success_at, last_error, last_verified_at, healthy, extra_json
        FROM vault_component_health
        WHERE vault_id=%s AND component=%s
        """,
        (int(vault_id), component),
    ).fetchone()
    last_success = stamp if ok else (existing["last_success_at"] if existing else None)
    last_error = None if ok else (error or "unknown error")
    last_verified = (
        stamp
        if verified and ok
        else (existing["last_verified_at"] if existing else None)
    )
    healthy = bool(ok) if extra is None or extra.get("healthy") is None else bool(
        extra.get("healthy")
    )
    if existing:
        connection.execute(
            """
            UPDATE vault_component_health
            SET last_success_at=%s, last_error=%s, last_verified_at=%s,
                healthy=%s, extra_json=%s, updated_at=%s
            WHERE vault_id=%s AND component=%s
            """,
            (
                last_success,
                last_error,
                last_verified,
                healthy,
                extra_json if extra_json is not None else existing["extra_json"],
                stamp,
                int(vault_id),
                component,
            ),
        )
        return
    connection.execute(
        """
        INSERT INTO vault_component_health(
            vault_id, component, last_success_at, last_error, last_verified_at,
            healthy, extra_json, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            int(vault_id),
            component,
            last_success,
            last_error,
            last_verified,
            healthy,
            extra_json,
            stamp,
        ),
    )


def load_vault_health(connection: Any, vault_id: int) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT component, last_success_at, last_error, last_verified_at,
               healthy, extra_json, updated_at
        FROM vault_component_health
        WHERE vault_id=%s
        """,
        (int(vault_id),),
    ).fetchall()
    health: dict[str, dict[str, Any]] = {}
    for row in rows:
        extra: dict[str, Any] = {}
        raw = row["extra_json"]
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                extra = parsed
        health[str(row["component"])] = {
            "last_success_at": row["last_success_at"],
            "last_error": row["last_error"],
            "last_verified_at": row["last_verified_at"],
            "healthy": None if row["healthy"] is None else bool(row["healthy"]),
            "extra": extra,
            "updated_at": row["updated_at"],
        }
    return health


def runtime_fields_from_health(
    health: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Scalar runtime overlay that survives process restart."""
    overlay: dict[str, Any] = {}
    for component in COMPONENTS:
        row = health.get(component) or {}
        overlay[f"last_error_{component}"] = row.get("last_error")
        overlay[f"last_success_{component}"] = row.get("last_success_at")
    cloud = health.get("cloud") or {}
    audit = health.get("audit") or {}
    overlay["last_verified_at"] = (
        audit.get("last_verified_at") or cloud.get("last_verified_at")
    )
    if audit:
        overlay["last_audit_healthy"] = audit.get("healthy")
        extra = audit.get("extra") or {}
        report = {
            key: extra[key] for key in AUDIT_REPORT_KEYS if key in extra
        }
        if report:
            overlay["last_audit_report"] = report
        if audit.get("last_success_at"):
            overlay["last_audit"] = audit.get("last_success_at")
    return overlay
