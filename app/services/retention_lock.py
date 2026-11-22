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
  stays an ordinary failure. Clocks differ: a date up to ``SKEW`` in the past
  still explains a refusal, and a deferral is never shorter than
  ``MIN_DEFERRAL`` (``locked_until``).
- **Unreadable**: when ``GetObjectRetention`` itself is refused (the IAM
  policy lacks it), the refusal cannot be explained. It is deferred by
  ``MIN_DEFERRAL`` instead of failing (``RetentionUnreadable``) and the Vault
  gets a Problem (``report_unreadable``), which closes once a probe can read a
  retention again. A Vault Bucket with a lock is created only after the same
  probe passed (``probe_retention_read``).

So a DELETE is sent only once the estimate has passed, and a refusal is
explained by the observed retention before anything fails.

A **Retained Version** is a row of ``retained_versions`` (migrations 0013,
0015): a superseded Archive Version kept in the cloud only because its
Retention Lock has not ended. A Current Snapshot cleanup hands it over
(``hand_over``); after a metadata restore the reconnect and the weekly audit
adopt the superseded locked versions the restored catalog no longer names
(``adopt_orphans``, database writes only). ``run_due`` deletes them by exact
VersionId once their date has passed, in batches. A Delete Marker on a locked
Vault Bucket (a DELETE without VersionId, never sent by FrostVault) opens a
Problem (``report_delete_markers``).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

# The choices offered when a Vault is created; 180 when the Owner enables it.
CHOICES = (30, 90, 180, 365)
MODE = "GOVERNANCE"

DELETE_MARKER_PROBLEM = "retention_lock_delete_marker"
# Retained Versions whose deletion kept failing (#749).
SWEEP_FAILED_PROBLEM = "retention_lock_sweep_failed"
# GetObjectRetention refused: a refusal could not be explained (#749).
UNREADABLE_PROBLEM = "retention_lock_unreadable"
# A Retained Version whose DELETE failed is tried again after this long; a
# transient S3 error (throttling, 5xx, network) after ``TRANSIENT_RETRY_SECONDS``.
RETRY_SECONDS = 24 * 60 * 60
TRANSIENT_RETRY_SECONDS = 60 * 60
# Consecutive failures of one Retained Version before its Vault gets a Problem.
FAILURES_BEFORE_PROBLEM = 3
# The background sweep of retained versions.
POLL_SECONDS = 60 * 60
# Rows per DeleteObjects request (the S3 maximum) and the time one sweep run
# may take before it leaves the rest for the next run.
SWEEP_BATCH = 1000
SWEEP_BUDGET_SECONDS = 5 * 60
# A claimed batch not recorded within this long (a crashed sweep) is due again.
CLAIM_SECONDS = 15 * 60
# Clock skew between FrostVault and S3 (#749): a RetainUntilDate this far in
# the past still explains a refusal, and a deferral lasts at least an hour.
SKEW = timedelta(minutes=5)
MIN_DEFERRAL = timedelta(seconds=POLL_SECONDS)
# The key read by the permission probe; no object is ever written there.
PROBE_KEY = ".frostvault-retention-probe"

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


class RetentionUnreadable(Exception):
    """S3 refused ``GetObjectRetention``: defer until ``until``, open a Problem."""

    def __init__(self, until: datetime) -> None:
        super().__init__("GetObjectRetention was refused")
        self.until = until


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


def locked_until(client: Any, *, bucket: str, key: str, version_id: str,
                 now: datetime) -> datetime | None:
    """Until when to defer a refused DELETE of a locked version, or None.

    The observed RetainUntilDate counts while it is later than ``now - SKEW``;
    the deferral is ``max(until, now + MIN_DEFERRAL)``. None: the version is
    not locked (an ordinary failure). Raises ``VersionGone`` when the version
    no longer exists and ``RetentionUnreadable`` when S3 refuses to say.
    """
    try:
        until = observed_until(client, bucket=bucket, key=key, version_id=version_id)
    except ClientError as exc:
        if error_code(exc) in _LOCK_REFUSALS:
            raise RetentionUnreadable(now + MIN_DEFERRAL) from None
        raise
    if until is None or until <= now - SKEW:
        return None
    return max(until, now + MIN_DEFERRAL)


def refused_until(client: Any, *, bucket: str, key: str, version_id: str, code: str,
                  now: datetime) -> datetime | None:
    """Why S3 refused a DELETE: until when a Retention Lock keeps the version.

    Only an ``AccessDenied`` is read (``locked_until``). None means the
    refusal is not explained by a Retention Lock (an ordinary failure). A
    version gone meanwhile raises ``VersionGone``, an unreadable retention
    ``RetentionUnreadable``.
    """
    if code not in _LOCK_REFUSALS:
        return None
    return locked_until(client, bucket=bucket, key=key, version_id=version_id, now=now)


def probe_retention_read(client: Any, bucket: str) -> str | None:
    """Whether FrostVault may read retentions in ``bucket``: None, or the refusal.

    Reads the retention of a key that does not exist (``PROBE_KEY``): with
    ``s3:GetObjectRetention`` S3 answers that it is absent, without it
    ``AccessDenied``. Nothing is written. Returns the refused error code.
    """
    try:
        client.get_object_retention(Bucket=bucket, Key=PROBE_KEY)
    except ClientError as exc:
        code = error_code(exc)
        if code in _GONE or code in _NOT_LOCKED:
            return None
        return code or "Error"
    return None


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
    """The Retained Versions of a Vault, for its status.

    ``failed_count``: rows whose last deletion failed, with the most recent
    error (``last_error``), the most consecutive failures of one row
    (``failed_attempts``), when it was tried and when it is tried again.
    """
    row = connection.execute(
        """SELECT count(*) AS n, COALESCE(sum(size), 0) AS bytes,
                  min(retain_until) AS first, max(retain_until) AS last,
                  count(*) FILTER (WHERE last_error IS NOT NULL) AS failed,
                  COALESCE(max(failures), 0) AS failures
           FROM retained_versions WHERE vault_id=%s""", (vault_id,)).fetchone()
    failing = connection.execute(
        """SELECT last_error, last_attempt_at, next_attempt_at FROM retained_versions
           WHERE vault_id=%s AND last_error IS NOT NULL
           ORDER BY last_attempt_at DESC NULLS LAST, id LIMIT 1""", (vault_id,)).fetchone()
    return {"count": int(row["n"] or 0), "bytes": int(row["bytes"] or 0),
            "deletable_from": iso(row["first"]), "deletable_until": iso(row["last"]),
            "failed_count": int(row["failed"] or 0),
            "failed_attempts": int(row["failures"] or 0),
            "last_error": str(failing["last_error"]) if failing else None,
            "last_attempt_at": iso(failing["last_attempt_at"]) if failing else None,
            "next_attempt_at": iso(failing["next_attempt_at"]) if failing else None}


def status(connection: Any, vault_id: int) -> dict[str, Any] | None:
    """The Vault's Retention Lock and what it holds back, or None without one."""
    days = vault_lock_days(connection, vault_id)
    if not days:
        return None
    return {"days": days, "mode": MODE, "retained": retained_summary(connection, vault_id)}


# -- adoption ------------------------------------------------------------------


def _latest_entry(items: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The newest listed version of a key: ``IsLatest``, else the one last
    modified; None when that is ambiguous."""
    flagged = [item for item in items if item.get("IsLatest") is True]
    if flagged:
        return flagged[0] if len(flagged) == 1 else None
    dated = sorted((item for item in items if item.get("LastModified") is not None),
                   key=lambda item: _aware(item["LastModified"]), reverse=True)
    if not dated or (len(dated) > 1
                     and _aware(dated[0]["LastModified"]) == _aware(dated[1]["LastModified"])):
        return None
    return dated[0]


def adopt_orphans(connection: Any, *, vault_id: int, days: int | None, prefix: str,
                  versions: Iterable[Mapping[str, Any]], marker_keys: Iterable[str]
                  ) -> set[tuple[str, str]]:
    """Adopt the superseded locked versions the catalog no longer names (#749).

    After a metadata restore the catalog predates the backup: a version
    superseded and handed over after it is in no table, so nothing would ever
    delete it. From a complete listing (``versions`` with ``Key``,
    ``VersionId``, ``IsLatest``, ``LastModified``, ``Size``, ``StorageClass``),
    a version becomes a Retained Version, with ``retain_until`` its
    ``LastModified`` plus the bucket default days, only when all of these hold:

    - it is not the newest entry of its key, and that newest entry is the
      Current of a Vault File (FrostVault wrote what superseded it);
    - its key has no Delete Marker (those are the Owner's to inspect) and is a
      Vault File path;
    - neither the catalog nor ``retained_versions`` names it, and its key has
      no unsettled upload or copy attempt, none whose Job still runs and no
      open upload remediation (a write still being reconciled).

    Database writes only; returns the adopted (key, VersionId). Nothing for a
    Vault without a Retention Lock.
    """
    from .vault_paths import object_key_to_path

    if not days:
        return set()
    markers = set(marker_keys)
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for item in versions:
        by_key.setdefault(str(item["Key"]), []).append(item)
    keys: list[str] = []
    version_ids: list[str] = []
    latest_ids: list[str] = []
    sizes: list[int] = []
    classes: list[str] = []
    until: list[datetime] = []
    for key, items in by_key.items():
        if key in markers or object_key_to_path(key, prefix) is None:
            continue
        latest = _latest_entry(items)
        if latest is None:
            continue
        for item in items:
            modified = _aware(item.get("LastModified"))
            if item is latest or modified is None or not item.get("VersionId"):
                continue
            keys.append(key)
            version_ids.append(str(item["VersionId"]))
            latest_ids.append(str(latest["VersionId"]))
            sizes.append(int(item.get("Size") or 0))
            classes.append(str(item.get("StorageClass") or "STANDARD"))
            until.append(modified + timedelta(days=int(days)))
    if not keys:
        return set()
    rows = connection.execute(
        """
        INSERT INTO retained_versions(vault_id, object_key, provider_version_id, size,
                                      storage_class, retain_until)
        SELECT %(vault)s, item.object_key, item.version_id, item.size, item.storage_class,
               item.retain_until
        FROM unnest(%(keys)s::text[], %(versions)s::text[], %(latest)s::text[],
                    %(sizes)s::bigint[], %(classes)s::text[], %(until)s::timestamptz[])
             AS item(object_key, version_id, latest_id, size, storage_class, retain_until)
        WHERE EXISTS (
                SELECT 1 FROM files f JOIN archive_versions cur ON cur.id = f.current_version_id
                WHERE f.vault_id = %(vault)s AND cur.object_key = item.object_key
                  AND cur.provider_version_id = item.latest_id)
          AND NOT EXISTS (
                SELECT 1 FROM archive_versions av JOIN files f ON f.id = av.file_id
                WHERE f.vault_id = %(vault)s AND av.object_key = item.object_key
                  AND av.provider_version_id = item.version_id)
          AND NOT EXISTS (
                SELECT 1 FROM upload_attempts a JOIN upload_snapshots s ON s.id = a.snapshot_id
                LEFT JOIN jobs j ON j.id = a.job_id
                WHERE s.vault_id = %(vault)s
                  AND vault_key_prefix(%(vault)s::bigint) || s.path = item.object_key
                  AND (a.settled_at IS NULL OR j.status NOT IN ('completed','failed','cancelled')))
          AND NOT EXISTS (
                SELECT 1 FROM copy_attempts c LEFT JOIN jobs j ON j.id = c.job_id
                WHERE c.vault_id = %(vault)s
                  AND item.object_key IN (c.source_key, c.destination_key)
                  AND (c.settled_at IS NULL OR j.status NOT IN ('completed','failed','cancelled')))
          AND NOT EXISTS (
                SELECT 1 FROM upload_remediations m JOIN files f ON f.id = m.file_id
                WHERE m.vault_id = %(vault)s AND m.closed_at IS NULL
                  AND vault_key_prefix(%(vault)s::bigint) || f.path = item.object_key)
        ON CONFLICT (vault_id, object_key, provider_version_id) DO NOTHING
        RETURNING object_key, provider_version_id
        """,
        {"vault": int(vault_id), "keys": keys, "versions": version_ids,
         "latest": latest_ids, "sizes": sizes, "classes": classes, "until": until},
    ).fetchall()
    adopted = {(str(r["object_key"]), str(r["provider_version_id"])) for r in rows}
    if adopted:
        from .audit_events import record_audit_event
        record_audit_event(connection, event="retention_lock.retained_adopted", actor="system",
                           vault_id=int(vault_id), outcome="success", adopted=len(adopted))
    return adopted


# -- sweep ---------------------------------------------------------------------

# A key with an open upload or copy attempt is skipped: a write in progress
# pins and lists its key, and must not see it change under it.
_CLAIM_SQL = """
WITH due AS (
    SELECT r.id FROM retained_versions r
    JOIN vaults v ON v.id = r.vault_id
    WHERE r.retain_until <= %(now)s
      AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= %(now)s)
      AND v.lifecycle <> 'provisioning' AND NOT v.paused
      AND NOT EXISTS (
            SELECT 1 FROM upload_attempts a JOIN upload_snapshots s ON s.id = a.snapshot_id
            WHERE a.settled_at IS NULL AND s.vault_id = r.vault_id
              AND vault_key_prefix(r.vault_id) || s.path = r.object_key)
      AND NOT EXISTS (
            SELECT 1 FROM copy_attempts c
            WHERE c.settled_at IS NULL AND c.vault_id = r.vault_id
              AND r.object_key IN (c.source_key, c.destination_key))
    ORDER BY r.retain_until, r.id
    LIMIT %(limit)s
    FOR UPDATE OF r SKIP LOCKED
)
UPDATE retained_versions r
SET next_attempt_at = %(claim)s, last_attempt_at = %(now)s, attempts = r.attempts + 1
FROM due, vaults v, vault_targets t
WHERE r.id = due.id AND v.id = r.vault_id AND t.id = v.target_id
RETURNING r.id, r.vault_id, r.object_key, r.provider_version_id, t.bucket,
          EXISTS (SELECT 1 FROM archive_versions av JOIN files f ON f.id = av.file_id
                  WHERE f.vault_id = r.vault_id AND av.object_key = r.object_key
                    AND av.provider_version_id = r.provider_version_id) AS catalogued
"""


def _claim(connection: Any, now: datetime, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Claim up to ``limit`` due rows; drop those the catalog names again."""
    rows = connection.execute(_CLAIM_SQL, {
        "now": now, "claim": now + timedelta(seconds=CLAIM_SECONDS), "limit": int(limit),
    }).fetchall()
    # The catalog names this version again (a restored catalog): it is no
    # longer garbage, and only the catalog decides about it.
    catalogued = [str(r["id"]) for r in rows if r["catalogued"]]
    if catalogued:
        connection.execute("DELETE FROM retained_versions WHERE id = ANY(%s::uuid[])", (catalogued,))
    return [dict(r) for r in rows if not r["catalogued"]], len(catalogued)


def _is_transient(error: BaseException | str) -> bool:
    from .upload_retry import _TRANSIENT_ERROR_CODES, is_provider_transient

    if isinstance(error, str):
        return error in _TRANSIENT_ERROR_CODES
    return is_provider_transient(error)


def _sweep_bucket(client: Any, bucket: str, rows: list[dict[str, Any]],
                  now: datetime) -> dict[str, Any]:
    """One DeleteObjects of exact versions, with no database connection held.

    Returns the rows by outcome: ``deleted`` ids, ``deferred`` (id, until),
    ``unreadable`` (id, until) and ``failed`` (id, error, transient).
    """
    from .s3_copy import _s3_version_already_gone

    result: dict[str, Any] = {"deleted": [], "deferred": [], "unreadable": [], "failed": []}
    try:
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": [
            {"Key": r["object_key"], "VersionId": r["provider_version_id"]} for r in rows],
            "Quiet": True})
    except Exception as exc:  # noqa: BLE001 - the outcome is unknown: retried
        error = (error_code(exc) or type(exc).__name__)[:200]
        transient = _is_transient(exc)
        result["failed"] = [(r["id"], error, transient) for r in rows]
        return result
    errors = {(str(e.get("Key") or ""), str(e.get("VersionId") or "")): str(e.get("Code") or "")
              for e in (response or {}).get("Errors") or []}
    for row in rows:
        code = errors.get((row["object_key"], row["provider_version_id"]))
        if code is None:
            result["deleted"].append(row["id"])
            continue
        if code in _GONE:
            result["deleted"].append(row["id"])
            continue
        try:
            until = refused_until(client, bucket=bucket, key=row["object_key"],
                                  version_id=row["provider_version_id"], code=code, now=now)
        except VersionGone:
            result["deleted"].append(row["id"])
            continue
        except RetentionUnreadable as exc:
            result["unreadable"].append((row["id"], exc.until))
            continue
        except Exception as exc:  # noqa: BLE001 - nothing decided: retried
            if _s3_version_already_gone(exc):
                result["deleted"].append(row["id"])
                continue
            result["failed"].append((row["id"], (code or "DeleteError")[:200], _is_transient(exc)))
            continue
        if until is not None:
            result["deferred"].append((row["id"], until))
        else:
            result["failed"].append((row["id"], (code or "DeleteError")[:200], _is_transient(code)))
    return result


def _record(connection: Any, result: dict[str, Any], now: datetime) -> None:
    if result["deleted"]:
        connection.execute("DELETE FROM retained_versions WHERE id = ANY(%s::uuid[])",
                           ([str(i) for i in result["deleted"]],))
    for row_id, until in result["deferred"]:
        # The lock explains the refusal: a later date, not a failure.
        connection.execute(
            """UPDATE retained_versions SET retain_until=%s, next_attempt_at=NULL, failures=0,
                   last_error=NULL WHERE id=%s""", (until, str(row_id)))
    for row_id, until in result["unreadable"]:
        connection.execute(
            "UPDATE retained_versions SET next_attempt_at=%s WHERE id=%s", (until, str(row_id)))
    for row_id, error, transient in result["failed"]:
        retry = TRANSIENT_RETRY_SECONDS if transient else RETRY_SECONDS
        connection.execute(
            """UPDATE retained_versions SET failures=failures + 1, last_error=%s,
                   next_attempt_at=%s WHERE id=%s""",
            (error, now + timedelta(seconds=retry), str(row_id)))


def _vault_params(connection: Any, vault_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT v.name, t.bucket FROM vaults v JOIN vault_targets t ON t.id = v.target_id
           WHERE v.id=%s""", (vault_id,)).fetchone()
    return {"vault": row["name"], "bucket": row["bucket"], "retention_lock": True} if row else None


def report_sweep_failures(connection: Any, vault_id: int) -> bool:
    """Open (or close) the Problem of Retained Versions that keep failing (#749).

    Open while one of them failed ``FAILURES_BEFORE_PROBLEM`` times in a row.
    """
    from .notifications import set_problem

    params = _vault_params(connection, vault_id)
    if params is None:
        return False
    row = connection.execute(
        """SELECT count(*) AS n, max(last_error) AS error FROM retained_versions
           WHERE vault_id=%s AND failures >= %s AND last_error IS NOT NULL""",
        (vault_id, FAILURES_BEFORE_PROBLEM)).fetchone()
    count = int(row["n"] or 0)
    set_problem(connection, key=f"{SWEEP_FAILED_PROBLEM}:{vault_id}", event=SWEEP_FAILED_PROBLEM,
                vault_id=vault_id, active=bool(count),
                params={**params, "count": count, "error": str(row["error"] or "")})
    return bool(count)


def report_unreadable(connection: Any, vault_id: int, *, active: bool = True) -> None:
    """Open (or close) the Problem of a retention FrostVault may not read (#749)."""
    from .notifications import set_problem

    params = _vault_params(connection, vault_id)
    if params is None:
        return
    set_problem(connection, key=f"{UNREADABLE_PROBLEM}:{vault_id}", event=UNREADABLE_PROBLEM,
                vault_id=vault_id, active=active, params=params)


def _recheck_unreadable(database: Database, client_factory: Callable[[], Any],
                        client: Any) -> Any:
    """Close the unreadable-retention Problems whose bucket can be read again.

    One probe per Vault with that Problem open, none otherwise.
    """
    with database() as connection:
        open_rows = connection.execute(
            """SELECT n.vault_id, t.bucket FROM notifications n
               JOIN vaults v ON v.id = n.vault_id JOIN vault_targets t ON t.id = v.target_id
               WHERE n.event=%s AND n.problem_status='open'""",
            (UNREADABLE_PROBLEM,)).fetchall()
    for row in open_rows:
        client = client or client_factory()
        try:
            refused = probe_retention_read(client, str(row["bucket"]))
        except Exception:  # noqa: BLE001 - unknown: the Problem stays open
            continue
        if refused is None:
            with database() as connection:
                report_unreadable(connection, int(row["vault_id"]), active=False)
    return client


def run_due(database: Database, client_factory: Callable[[], Any], *,
            now: datetime | None = None, limit: int = SWEEP_BATCH,
            budget_seconds: float = SWEEP_BUDGET_SECONDS,
            clock: Callable[[], float] = time.monotonic) -> dict[str, int]:
    """Delete the Retained Versions whose RetainUntilDate has passed.

    Batches of up to ``limit`` due rows are claimed in one short transaction
    (``next_attempt_at`` moves past the claim) and deleted with one
    ``DeleteObjects`` per bucket, by exact VersionId, while no database
    connection is held; then the outcome is recorded. Batches follow each
    other until nothing is due or ``budget_seconds`` have passed. A refusal
    explained by a later RetainUntilDate moves the row to that date, an
    unreadable retention waits an hour with a Problem, any other failure is
    retried after an hour (transient) or a day, and three in a row open a
    Problem. A paused Vault and a key with an open upload or copy attempt wait.
    """
    moment = now or datetime.now(timezone.utc)
    started = clock()
    outcomes: dict[str, int] = {}
    client = _recheck_unreadable(database, client_factory, None)
    deleted: dict[int, int] = {}
    touched: set[int] = set()
    unreadable: set[int] = set()
    while True:
        with database() as connection:
            rows, catalogued = _claim(connection, moment, limit)
        if catalogued:
            outcomes["catalogued"] = outcomes.get("catalogued", 0) + catalogued
        if not rows:
            break
        client = client or client_factory()
        by_bucket: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_bucket.setdefault(str(row["bucket"]), []).append(row)
        vault_of = {row["id"]: int(row["vault_id"]) for row in rows}
        results = [_sweep_bucket(client, bucket, items, moment)
                   for bucket, items in sorted(by_bucket.items())]
        with database() as connection:
            for result in results:
                _record(connection, result, moment)
        for result in results:
            for outcome in ("deleted", "deferred", "unreadable", "failed"):
                if result[outcome]:
                    outcomes[outcome] = outcomes.get(outcome, 0) + len(result[outcome])
            for row_id in result["deleted"]:
                deleted[vault_of[row_id]] = deleted.get(vault_of[row_id], 0) + 1
            unreadable.update(vault_of[row_id] for row_id, _ in result["unreadable"])
        touched.update(vault_of.values())
        if len(rows) + catalogued < limit or clock() - started >= budget_seconds:
            break
    if touched:
        from .audit_events import record_audit_event
        with database() as connection:
            for vault_id, count in sorted(deleted.items()):
                record_audit_event(connection, event="retention_lock.retained_deleted",
                                   actor="system", vault_id=vault_id, outcome="success",
                                   deleted=count)
            for vault_id in sorted(touched):
                report_sweep_failures(connection, vault_id)
            for vault_id in sorted(unreadable):
                report_unreadable(connection, vault_id)
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
