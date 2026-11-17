"""Retention Lock: Governance-mode Object Lock of a Vault Bucket (#736, ADR-0050).

A Vault may be created with a Retention Lock: its Vault Bucket gets S3 Object
Lock in Governance mode with a default retention of 30, 90, 180 (the default)
or 365 days. Every Archive Version written to it is locked from its creation
until its ``RetainUntilDate``. FrostVault never holds or sends
``s3:BypassGovernanceRetention`` (the IAM policy denies it explicitly), so no
deletion path can remove a locked version early: each one *defers* it, never
fails it.

Where the RetainUntilDate comes from:

- **Estimate** (``estimated_until``): the version's creation plus the bucket
  default days. For a listed version the creation is its ``LastModified``
  (exact); for a catalogued one the Archive Version's ``created_at``, written
  after S3 answered the PUT or copy, so never earlier than the real creation.
  It is used for previews and to skip a DELETE that would certainly be refused.
  It is wrong only if someone outside FrostVault changed a version's retention
  or the bucket default: a shortened one is merely deleted later than possible,
  an extended one makes the DELETE refused, which the observation explains.
- **Observation** (``observed_until``): ``GetObjectRetention`` of the exact
  VersionId, read when a DELETE whose estimate had passed is refused. Its date
  is the one shown and waited for. A refusal on a version that is not locked
  stays an ordinary failure.

So a DELETE is sent only once the estimate has passed, and a refusal is
explained by the observed retention before anything fails.

``retained_versions`` (migration 0013) holds the superseded versions a Current
Snapshot cleanup could not delete yet; ``run_due`` deletes them by exact
VersionId once their date has passed. A Delete Marker on a locked Vault Bucket
(a DELETE without VersionId, never sent by FrostVault) opens a Problem
(``report_delete_markers``).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

# The choices offered when a Vault is created; 180 when the Owner enables it.
CHOICES = (30, 90, 180, 365)
DEFAULT_DAYS = 180
MODE = "GOVERNANCE"

DELETE_MARKER_PROBLEM = "retention_lock_delete_marker"
# A retained version whose DELETE failed for another reason is tried again
# after this long.
RETRY_SECONDS = 24 * 60 * 60
# The background sweep of retained versions.
POLL_SECONDS = 60 * 60
# Rows deleted per sweep run, so one run stays short.
SWEEP_BATCH = 500

# Error codes that say a version is not locked at all.
_NOT_LOCKED = frozenset({"NoSuchObjectLockConfiguration", "ObjectLockConfigurationNotFoundError",
                         "InvalidRequest"})
_GONE = frozenset({"NoSuchKey", "NoSuchVersion", "NotFound", "404"})
# What S3 answers to a DELETE of a locked version without the bypass.
_LOCK_REFUSALS = frozenset({"AccessDenied"})

Database = Callable[[], AbstractContextManager[Any]]
log = logging.getLogger(__name__)


class VersionGone(Exception):
    """The exact version no longer exists."""


def validate_days(days: int | None) -> int | None:
    """None (no Retention Lock) or one of the offered durations."""
    if days is None:
        return None
    if isinstance(days, bool) or int(days) not in CHOICES:
        raise ValueError("A Retention Lock lasts 30, 90, 180 or 365 days")
    return int(days)


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def iso(value: Any) -> str | None:
    moment = _aware(value)
    return moment.isoformat() if moment else None


def estimated_until(created_at: Any, days: int | None) -> datetime | None:
    """The default RetainUntilDate of a version created at ``created_at``."""
    created = _aware(created_at)
    if not days or created is None:
        return None
    return created + timedelta(days=int(days))


def locked_at(created_at: Any, days: int | None, at: datetime) -> datetime | None:
    """The estimated RetainUntilDate when it is still ahead of ``at``, else None."""
    until = estimated_until(created_at, days)
    return until if until is not None and until > at else None


def error_code(exc: BaseException) -> str:
    if not isinstance(exc, ClientError):
        return ""
    return str((exc.response or {}).get("Error", {}).get("Code") or "")


def observed_until(client: Any, *, bucket: str, key: str, version_id: str) -> datetime | None:
    """The exact version's RetainUntilDate from ``GetObjectRetention``, or None.

    None when the version carries no retention. Raises ``VersionGone`` when the
    version does not exist; any other error propagates (nothing is decided).
    """
    try:
        response = client.get_object_retention(Bucket=bucket, Key=key, VersionId=version_id)
    except ClientError as exc:
        code = error_code(exc)
        if code in _NOT_LOCKED:
            return None
        if code in _GONE:
            raise VersionGone(key) from None
        raise
    retention = (response or {}).get("Retention") or {}
    return _aware(retention.get("RetainUntilDate"))


def refused_until(client: Any, *, bucket: str, key: str, version_id: str, code: str,
                  now: datetime) -> datetime | None:
    """Why S3 refused a DELETE: the RetainUntilDate when the version is locked.

    None means the refusal is not explained by a Retention Lock (an ordinary
    failure). A version gone meanwhile raises ``VersionGone``.
    """
    if code not in _LOCK_REFUSALS:
        return None
    until = observed_until(client, bucket=bucket, key=key, version_id=version_id)
    return until if until is not None and until > now else None


def summarize(rows: Iterable[dict[str, Any]], *, days: int | None, at: datetime,
              created: str = "created_at", size: str = "size") -> dict[str, Any] | None:
    """How many of ``rows`` are still locked at ``at``, how big, until when.

    The dates are estimates (creation + bucket default days). None for a Vault
    without a Retention Lock.
    """
    if not days:
        return None
    count = total = 0
    first: datetime | None = None
    last: datetime | None = None
    for row in rows:
        until = locked_at(row.get(created), days, at)
        if until is None:
            continue
        count += 1
        total += int(row.get(size) or 0)
        first = until if first is None or until < first else first
        last = until if last is None or until > last else last
    return {"days": int(days), "mode": MODE, "locked_count": count, "locked_bytes": total,
            "locked_from": iso(first), "locked_until": iso(last)}


def vault_lock_days(connection: Any, vault_id: int) -> int | None:
    row = connection.execute(
        """SELECT t.retention_lock_days FROM vaults v JOIN vault_targets t ON t.id = v.target_id
           WHERE v.id=%s""", (vault_id,)).fetchone()
    return int(row["retention_lock_days"]) if row and row["retention_lock_days"] else None


# -- retained versions ---------------------------------------------------------


def retained_at_keys(connection: Any, vault_id: int, keys: Iterable[str]) -> set[tuple[str, str]]:
    """The exact retained versions (key, VersionId) at ``keys``."""
    names = sorted(set(keys))
    if not names:
        return set()
    rows = connection.execute(
        """SELECT object_key, provider_version_id FROM retained_versions
           WHERE vault_id=%s AND object_key = ANY(%s::text[])""", (vault_id, names)).fetchall()
    return {(str(r["object_key"]), str(r["provider_version_id"])) for r in rows}


def retained_versions(connection: Any, vault_id: int) -> set[tuple[str, str]]:
    """Every retained version (key, VersionId) of a Vault."""
    rows = connection.execute(
        "SELECT object_key, provider_version_id FROM retained_versions WHERE vault_id=%s",
        (vault_id,)).fetchall()
    return {(str(r["object_key"]), str(r["provider_version_id"])) for r in rows}


def hand_over(connection: Any, *, vault_id: int, object_key: str, version_id: str,
              size: int, storage_class: str | None, retain_until: datetime) -> None:
    """Record a locked superseded version whose deletion waits for ``retain_until``."""
    connection.execute(
        """INSERT INTO retained_versions(vault_id, object_key, provider_version_id, size,
                                         storage_class, retain_until)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (vault_id, object_key, provider_version_id)
           DO UPDATE SET retain_until = GREATEST(retained_versions.retain_until,
                                                 EXCLUDED.retain_until)""",
        (vault_id, object_key, version_id, int(size or 0), storage_class, retain_until))


def retained_summary(connection: Any, vault_id: int) -> dict[str, Any]:
    """The retained versions of a Vault, for its status."""
    row = connection.execute(
        """SELECT count(*) AS n, COALESCE(sum(size), 0) AS bytes,
                  min(retain_until) AS first, max(retain_until) AS last
           FROM retained_versions WHERE vault_id=%s""", (vault_id,)).fetchone()
    return {"count": int(row["n"] or 0), "bytes": int(row["bytes"] or 0),
            "deletable_from": iso(row["first"]), "deletable_until": iso(row["last"])}


def status(connection: Any, vault_id: int) -> dict[str, Any] | None:
    """The Vault's Retention Lock and what it holds back, or None without one."""
    days = vault_lock_days(connection, vault_id)
    if not days:
        return None
    return {"days": days, "mode": MODE, "retained": retained_summary(connection, vault_id)}


def _due(connection: Any, now: datetime, limit: int) -> list[dict[str, Any]]:
    return connection.execute(
        """SELECT r.id, r.vault_id, r.object_key, r.provider_version_id, t.bucket
           FROM retained_versions r
           JOIN vaults v ON v.id = r.vault_id
           JOIN vault_targets t ON t.id = v.target_id
           WHERE r.retain_until <= %s
             AND (r.last_attempt_at IS NULL OR r.last_attempt_at <= %s)
             AND v.lifecycle <> 'provisioning' AND NOT v.paused
           ORDER BY r.retain_until, r.id LIMIT %s""",
        (now, now - timedelta(seconds=RETRY_SECONDS), limit)).fetchall()


def _delete_one(database: Database, client: Any, row: dict[str, Any], now: datetime) -> str:
    """Delete one due retained version by exact VersionId; returns the outcome."""
    from .s3_copy import _s3_version_already_gone

    with database() as connection:
        # The Vault row first (the repository-wide lock order), then the row.
        connection.execute("SELECT id FROM vaults WHERE id=%s FOR SHARE", (row["vault_id"],))
        live = connection.execute(
            "SELECT * FROM retained_versions WHERE id=%s FOR UPDATE SKIP LOCKED",
            (row["id"],)).fetchone()
        if live is None or _aware(live["retain_until"]) > now:
            return "skipped"
        if connection.execute(
                """SELECT 1 FROM archive_versions av JOIN files f ON f.id = av.file_id
                   WHERE f.vault_id=%s AND av.object_key=%s AND av.provider_version_id=%s""",
                (row["vault_id"], row["object_key"], row["provider_version_id"])).fetchone():
            # The catalog names this version again (a restored catalog): it is
            # no longer garbage, and only the catalog decides about it.
            connection.execute("DELETE FROM retained_versions WHERE id=%s", (row["id"],))
            return "catalogued"
        try:
            client.delete_object(Bucket=row["bucket"], Key=row["object_key"],
                                 VersionId=row["provider_version_id"])
        except Exception as exc:
            if _s3_version_already_gone(exc):
                connection.execute("DELETE FROM retained_versions WHERE id=%s", (row["id"],))
                return "deleted"
            code = error_code(exc)
            try:
                until = refused_until(client, bucket=row["bucket"], key=row["object_key"],
                                      version_id=row["provider_version_id"], code=code, now=now)
            except VersionGone:
                connection.execute("DELETE FROM retained_versions WHERE id=%s", (row["id"],))
                return "deleted"
            except Exception:
                until = None
            if until is not None:
                connection.execute(
                    """UPDATE retained_versions SET retain_until=%s, attempts=attempts + 1,
                           last_attempt_at=NULL, last_error=NULL WHERE id=%s""",
                    (until, row["id"]))
                return "deferred"
            connection.execute(
                """UPDATE retained_versions SET attempts=attempts + 1, last_attempt_at=%s,
                       last_error=%s WHERE id=%s""",
                (now, (code or type(exc).__name__)[:200], row["id"]))
            return "failed"
        connection.execute("DELETE FROM retained_versions WHERE id=%s", (row["id"],))
        return "deleted"


def run_due(database: Database, client_factory: Callable[[], Any], *,
            now: datetime | None = None, limit: int = SWEEP_BATCH) -> dict[str, int]:
    """Delete the retained versions whose RetainUntilDate has passed.

    Each DELETE names the exact VersionId and runs while its row is locked; a
    refusal explained by a later RetainUntilDate moves the row to that date,
    any other failure is retried after a day. A paused Vault waits.
    """
    moment = now or datetime.now(timezone.utc)
    with database() as connection:
        due = _due(connection, moment, limit)
    outcomes: dict[str, int] = {}
    if not due:
        return outcomes
    client = client_factory()
    deleted: dict[int, int] = {}
    for row in due:
        outcome = _delete_one(database, client, row, moment)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome == "deleted":
            deleted[int(row["vault_id"])] = deleted.get(int(row["vault_id"]), 0) + 1
    if deleted:
        from .audit_events import record_audit_event
        with database() as connection:
            for vault_id, count in sorted(deleted.items()):
                record_audit_event(connection, event="retention_lock.retained_deleted",
                                   actor="system", vault_id=vault_id, outcome="success",
                                   deleted=count)
    return outcomes


# -- audit ---------------------------------------------------------------------


def report_delete_markers(connection: Any, vault: dict[str, Any],
                          markers: Iterable[tuple[str, str]]) -> int:
    """Open (or close) the Problem of Delete Markers on a locked Vault Bucket.

    FrostVault always deletes by exact VersionId and never creates a Delete
    Marker. On a bucket with a Retention Lock a marker means a DELETE without
    VersionId by someone else (a stolen key, a script): the versions are still
    there, hidden behind it. Returns the number of markers reported.
    """
    from .notifications import set_problem

    if not vault.get("s3_retention_lock_days"):
        return 0
    found = sorted(markers)
    set_problem(connection, key=f"{DELETE_MARKER_PROBLEM}:{vault['id']}",
                event=DELETE_MARKER_PROBLEM, vault_id=int(vault["id"]), active=bool(found),
                params={"vault": vault["name"], "bucket": vault["s3_bucket"],
                        "count": len(found), "first_key": found[0][0] if found else "",
                        "retention_lock": True})
    return len(found)
