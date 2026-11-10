"""Installation-wide S3 readiness; a new process never inherits ready authority.

The advisory probe lock is held without Job/Vault locks. Read-only provider I/O
is outside replayable DB units; publication retries retain the probe outcome.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError
from ..i18n import translate
from . import notifications
from .s3_preflight import BUCKET_NOT_FOUND_KEY
from .database_retry import exception_chain, owned_connection, retry_database_unit

LOCK_ID = 461062
MAX_REPROBE_SECONDS = 60
_ready_pid: int | None = None
AUTH_REMEDIATION = "S3 access was refused. Check AWS credentials and required bucket permissions; Jobs remain queued"


# Readiness failures whose stored remediation is the English text of a catalog
# key. The state row keeps that text; readers localize it per viewer.
LOCALIZED_REMEDIATION_KEYS = (BUCKET_NOT_FOUND_KEY,)


def remediation_key(remediation: str | None) -> str | None:
    """Catalog key whose English text is exactly this stored remediation."""
    if not remediation:
        return None
    return next((key for key in LOCALIZED_REMEDIATION_KEYS
                 if translate(key, locale="en") == remediation), None)


def localized_snapshot(state: dict[str, Any], locale: str | None) -> dict[str, Any]:
    key = remediation_key(state.get("remediation"))
    return {**state, "remediation": translate(key, locale=locale)} if key else state


class CloudPreflightBlocked(RuntimeError):
    """Queue suspension, NOT a Job failure or a transfer retry."""


class ReadinessFailure(RuntimeError):
    """Only locally composed, safe remediation text may use this exception."""


def reprobe_seconds(failures: int) -> int:
    return min(MAX_REPROBE_SECONDS, 5 * 2 ** min(max(failures - 1, 0), 4))


def snapshot(connection: Any) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM cloud_preflight_state WHERE id=1").fetchone()
    return dict(row) if row else {"status": "unknown", "remediation": None, "next_probe_at": None}


def probe_due(state: dict[str, Any], now: datetime) -> bool:
    """Clock rollback/restored future timestamps cannot suspend the queue forever."""
    try:
        delay = (datetime.fromisoformat(state["next_probe_at"]) - now).total_seconds()
    except (KeyError, TypeError, ValueError):
        return True
    return not 0 < delay <= MAX_REPROBE_SECONDS


def dispatch_ready(state: dict[str, Any], now: datetime) -> bool:
    return (_ready_pid == os.getpid() and state['status'] == 'ready'
            and not probe_due(state, now))


# S3 answers HTTP 403 ``InvalidObjectState`` to a GET of a version in cold storage
# (Glacier / Deep Archive) that was not restored. It is the object's state, not a
# credential or permission problem: it never reaches the readiness gate.
COLD_STORAGE_CODES = frozenset({'InvalidObjectState'})


def is_cold_storage_error(error: BaseException) -> bool:
    return any(isinstance(item, ClientError)
               and item.response.get('Error', {}).get('Code') in COLD_STORAGE_CODES
               for item in exception_chain(error))


def is_readiness_error(error: BaseException) -> bool:
    """Only structured provider auth failures, including SDK/service wrappers."""
    return any(isinstance(item, ClientError)
               and item.response.get('Error', {}).get('Code') not in COLD_STORAGE_CODES
               and (
        item.response.get('Error', {}).get('Code') in {
            'InvalidAccessKeyId', 'SignatureDoesNotMatch', 'AccessDenied',
            'AccessDeniedException', 'ExpiredToken', 'InvalidToken', 'TokenRefreshRequired',
            'InvalidClientTokenId', 'UnrecognizedClientException',
        } or item.response.get('ResponseMetadata', {}).get('HTTPStatusCode') in {401, 403}
    ) for item in exception_chain(error))


def _problem_params(remediation: str | None) -> dict[str, str]:
    params = {'remediation': remediation or ''}
    key = remediation_key(remediation)
    if key:
        params['remediation_key'] = key
    return params


@retry_database_unit
def _publish(connect: Callable, *, remediation: str | None, failures: int, due: str, stamp: str) -> dict[str, Any]:
    with owned_connection(connect()) as connection:
        connection.execute(
            "INSERT INTO cloud_preflight_state(id,status,remediation,failure_count,next_probe_at) "
            "VALUES (1,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status, "
            "remediation=EXCLUDED.remediation,failure_count=EXCLUDED.failure_count,next_probe_at=EXCLUDED.next_probe_at",
            ('blocked' if remediation else 'ready', remediation, failures, due),
        )
        notifications.set_problem(
            connection, key='blocked:installation:preflight', event='cloud_preflight_blocked',
            active=bool(remediation), params=_problem_params(remediation),
        )
        # Supersede pre-461 per-Vault episodes, not Job failure history.
        connection.execute(
            "UPDATE notifications SET problem_status='resolved',resolved_at=%s,read_at=COALESCE(read_at,%s) "
            "WHERE problem_status='open' AND dedupe_key LIKE 'blocked:%%:preflight' "
            "AND dedupe_key<>'blocked:installation:preflight'", (stamp, stamp),
        )
        return snapshot(connection)


@retry_database_unit
def invalidate(connect: Callable) -> None:
    """Fence stale ready, requesting an immediate independent probe.

    Share the probe lock so an older in-flight probe cannot overwrite this
    refusal. Call after the failing unit has closed, with no Job/Vault locks.
    Concurrent refusals preserve an already-blocked gate's backoff.
    """
    global _ready_pid
    _ready_pid = None
    stamp = datetime.now(timezone.utc).isoformat()
    with owned_connection(connect()) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
        changed = connection.execute(
            "INSERT INTO cloud_preflight_state(id,status,remediation,failure_count,next_probe_at) "
            "VALUES (1,'blocked',%s,0,%s) ON CONFLICT(id) DO UPDATE SET "
            "status='blocked',remediation=EXCLUDED.remediation,next_probe_at=EXCLUDED.next_probe_at "
            "WHERE cloud_preflight_state.status<>'blocked' RETURNING id", (AUTH_REMEDIATION, stamp),
        ).fetchone()
        if changed:
            notifications.set_problem(connection, key='blocked:installation:preflight',
                event='cloud_preflight_blocked', active=True, params={'remediation': AUTH_REMEDIATION})


def check(connect: Callable, probe: Callable[[], None], *, now: datetime | None = None) -> dict[str, Any]:
    """Return a committed outcome; never wait behind another read-only probe.

    Blocked backoff survives restart. Ready may be reused only after this process
    probes successfully, because deployment credentials may have changed.
    """
    global _ready_pid
    injected_now = now
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    with owned_connection(connect()) as connection:
        state = snapshot(connection)
        needs_startup = _ready_pid != os.getpid() and state['status'] == 'ready'
        if not needs_startup and not probe_due(state, injected_now or datetime.now(timezone.utc)):
            return state
        acquired = connection.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS acquired", (LOCK_ID,),
        ).fetchone()["acquired"]
        if not acquired:
            return {**state, "status": "blocked", "remediation": state.get("remediation") or
                    "S3 readiness check in progress; Jobs remain queued"}
        state = snapshot(connection)
        needs_startup = _ready_pid != os.getpid() and state['status'] == 'ready'
        if not needs_startup and not probe_due(state, injected_now or datetime.now(timezone.utc)):
            return state
        remediation = None
        try:
            probe()
        except ReadinessFailure as exc:
            remediation = str(exc)
        except Exception:
            remediation = "S3 readiness could not be confirmed. Check AWS credentials, bucket configuration and connectivity"
        failures = int(state.get("failure_count") or 0) + 1 if remediation else 0
        completed = injected_now or datetime.now(timezone.utc)
        due = (completed + timedelta(seconds=reprobe_seconds(failures) if remediation else MAX_REPROBE_SECONDS)).isoformat()
        result = _publish(connect, remediation=remediation, failures=failures, due=due, stamp=stamp)
    # Only a committed successful publication validates this process.
    _ready_pid = os.getpid() if not remediation else None
    return result
