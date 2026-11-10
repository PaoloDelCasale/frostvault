"""Owner notifications with fixed routing and optional deployment-owned SMTP.

Delivery retries are bounded and never mutate the Job that triggered the
notification. Problems have a current open/resolved state independent of reading.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Protocol

from ..config import settings
from ..i18n import catalog as locale_catalog
from ..i18n import format_message_params, parse_message_params, translate


# Upload failures beyond GROUP_THRESHOLD per Vault inside GROUP_WINDOW_SECONDS fold
# into one open "job_failed_group" problem instead of one row per file (#499).
GROUP_THRESHOLD = 10
GROUP_WINDOW_SECONDS = 600

# SQL (alias ``original`` = a failed upload Job): the file no longer exists and
# the Job never reached the cloud, so the failure has nothing left to protect.
# Failures that left a materialized fact (linked version, started send, a
# remediation episode) are excluded: they stay open until reconciled.
REMOVED_FILE_FAILURE_SQL = """(
    original.action='upload' AND original.status='failed'
    AND original.archive_version_id IS NULL
    AND EXISTS (SELECT 1 FROM local_copies lc
                WHERE lc.vault_file_id=original.vault_file_id AND lc.presence='missing')
    AND NOT EXISTS (SELECT 1 FROM upload_intent_jobs ib
                    JOIN upload_intents i ON i.id=ib.intent_id
                    WHERE ib.job_id=original.id AND i.send_started)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs rj WHERE rj.job_id=original.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_originals ro WHERE ro.original_job_id=original.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_discovery q WHERE q.job_id=original.id)
)"""

DELIVERY_TIMEOUT_SECONDS = 10.0
DELIVERY_RETRY_CAP_SECONDS = 300
_logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SmtpClient(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...


def _message_params(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("message_params")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) or raw is None:
        return parse_message_params(raw)
    return {}


def _localized_params(params: Mapping[str, Any], locale: str | None) -> dict[str, Any]:
    """Translate a keyed ``remediation`` param; its stored text stays English."""
    localized = dict(params)
    key = localized.get("remediation_key")
    if isinstance(key, str) and key in locale_catalog("en"):
        localized["remediation"] = translate(key, locale=locale)
    return localized


def _localized_notification_text(
    row: Mapping[str, Any], *, key_field: str, text_field: str, locale: str | None
) -> str:
    """Render a keyed notification, retaining prose for historical rows.

    A key can be removed or unavailable in a partially upgraded catalog.  In
    that case the durable title/body is safer than returning the key itself.
    """
    if row.get("event") == "weekly_summary" and text_field == "body":
        return render_weekly_summary(_message_params(row), locale=locale)
    key = row.get(key_field)
    fallback = str(row.get(text_field) or "")
    if not key:
        return fallback
    key = str(key)
    resolved_catalog = locale_catalog(locale)
    if key not in resolved_catalog:
        # ``translate`` normally falls back to English, so check that catalog
        # explicitly before deciding that this is an unknown new key.
        english_catalog = locale_catalog("en")
        if key not in english_catalog:
            return fallback
    return translate(key, locale=locale, **_localized_params(_message_params(row), locale))


def render_weekly_summary(summary: Mapping[str, Any], *, locale: str | None = None) -> str:
    """Render the same structured summary in either supported language."""
    states = summary.get("states") or {}
    counts = " · ".join(f"{translate('state.' + state, locale=locale)}: {count}"
                        for state, count in states.items())
    lines = [counts, translate("notification.weekly_summary.metrics", locale=locale,
             backup=summary.get("last_verified_backup") or "—", problems=summary.get("open_problems", 0),
             files=summary.get("freed_files", 0), gib=f"{int(summary.get('freed_bytes', 0)) / 1024**3:.2f}",
             cost=f"{summary.get('monthly_cost_estimate', 0):.2f}", currency=summary.get("currency", "EUR"),
             blocked=summary.get("cleanup_blocked", 0))]
    for vault in summary.get("vaults") or []:
        lines.append(translate("notification.weekly_summary.vault", locale=locale,
                     vault=vault["vault_id"], scan=vault.get("last_scan") or "—",
                     audit=vault.get("last_audit") or "—", suspended=vault["suspended"]))
    for problem in summary.get("problems") or []:
        lines.append(translate(problem["body_key"], locale=locale,
                               **_localized_params(parse_message_params(problem.get("message_params")), locale))
                     if problem.get("body_key") else problem["event"])
    lines.append(str(summary.get("cost_disclaimer") or ""))
    return "\n".join(lines)


def _row_notification(
    row: dict[str, Any], *, locale: str | None = None
) -> dict[str, Any]:
    params = _message_params(row)
    in_app_value = row.get("in_app_enabled")
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "vault_id": row.get("vault_id"),
        "job_id": row.get("job_id"),
        "event": row["event"],
        "title": _localized_notification_text(
            row, key_field="title_key", text_field="title", locale=locale
        ),
        "body": _localized_notification_text(
            row, key_field="body_key", text_field="body", locale=locale
        ),
        "title_key": row.get("title_key"),
        "body_key": row.get("body_key"),
        "message_params": params,
        "in_app_enabled": False if in_app_value is False or in_app_value == 0 else True,
        "dedupe_key": row.get("dedupe_key"),
        "created_at": row["created_at"],
        "read": row.get("read_at") is not None,
        "read_at": row.get("read_at"),
        "problem_status": row.get("problem_status"),
        "resolved_at": row.get("resolved_at"),
    }


def _owner_recipients(connection: Any) -> list[dict[str, Any]]:
    """The single owner is the only notification recipient (#412)."""
    from ..owner import owner_user_id

    owner_id = owner_user_id(connection)
    return [] if owner_id is None else [{"user_id": owner_id}]


def _ensure_delivery(
    connection: Any, *, notification_id: int, channel: str, stamp: str
) -> None:
    if channel == "in_app":
        return
    if channel != "email":
        raise ValueError(f"invalid delivery channel: {channel}")
    existing = connection.execute(
        """
        SELECT id FROM notification_deliveries
        WHERE notification_id=%s AND channel=%s
        ORDER BY id
        LIMIT 1
        """,
        (notification_id, channel),
    ).fetchone()
    if existing:
        return
    connection.execute(
        """
        INSERT INTO notification_deliveries(
            notification_id, channel, status, attempt_count,
            next_attempt_at, updated_at
        ) VALUES (%s, %s, 'pending', 0, %s, %s)
        """,
        (notification_id, channel, stamp, stamp),
    )


def enqueue_notification(
    connection: Any,
    *,
    user_id: int,
    event: str,
    title: str,
    body: str = "",
    vault_id: int | None = None,
    job_id: int | None = None,
    channels: tuple[str, ...] = ("in_app",),
    title_key: str | None = None,
    body_key: str | None = None,
    message_params: Mapping[str, Any] | None = None,
    in_app_enabled: bool | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Persist one notification and attach any requested outbound channels.

    ``in_app_enabled`` preserves visibility for historical email-only rows.
    ``dedupe_key`` is optional for legacy callers and unique per User when set.
    """
    requested_channels = tuple(dict.fromkeys(channels))
    for channel in requested_channels:
        if channel not in {"in_app", "email"}:
            raise ValueError(f"invalid delivery channel: {channel}")
    params = dict(message_params or {})
    if title_key and not title:
        title = translate(title_key, **params)
    if body_key and not body:
        body = translate(body_key, **params)
    effective_in_app = (
        bool(in_app_enabled)
        if in_app_enabled is not None
        else "in_app" in requested_channels
    )
    stamp = now_iso()
    params_json = (
        format_message_params(params) if title_key or body_key or params else None
    )
    if event == "weekly_summary":
        body = render_weekly_summary(params)

    if dedupe_key is None:
        row = connection.execute(
            """
            INSERT INTO notifications(
                user_id, vault_id, job_id, event, title, body,
                title_key, body_key, message_params, in_app_enabled,
                dedupe_key, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                user_id,
                vault_id,
                job_id,
                event,
                title,
                body,
                title_key,
                body_key,
                params_json,
                effective_in_app,
                dedupe_key,
                stamp,
            ),
        ).fetchone()
    else:
        row = connection.execute(
            """
            INSERT INTO notifications(
                user_id, vault_id, job_id, event, title, body,
                title_key, body_key, message_params, in_app_enabled,
                dedupe_key, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(user_id, dedupe_key) DO NOTHING
            RETURNING *
            """,
            (
                user_id,
                vault_id,
                job_id,
                event,
                title,
                body,
                title_key,
                body_key,
                params_json,
                effective_in_app,
                dedupe_key,
                stamp,
            ),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT * FROM notifications
                WHERE user_id=%s AND dedupe_key=%s
                ORDER BY id
                LIMIT 1
                """,
                (user_id, dedupe_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("Notification deduplication lookup failed")
            if effective_in_app and not bool(row.get("in_app_enabled", True)):
                connection.execute(
                    """
                    UPDATE notifications SET in_app_enabled=TRUE
                    WHERE id=%s
                    """,
                    (row["id"],),
                )
                row = connection.execute(
                    "SELECT * FROM notifications WHERE id=%s", (row["id"],)
                ).fetchone()

    notification = _row_notification(row)
    for channel in requested_channels:
        _ensure_delivery(
            connection,
            notification_id=int(notification["id"]),
            channel=channel,
            stamp=stamp,
        )
    return notification


def enqueue_recovery_approval_best_effort(connection: Any, *, job_ids: list[int]) -> None:
    """One inbox/optional email notice per request, isolated from admission."""
    from ..database import transaction
    try:
        with transaction(connection):
            job = connection.execute(
                """SELECT vault_id, group_id, min(path) AS path, count(*) AS count,
                          max(estimated_cost_eur) AS cost
                   FROM jobs WHERE id=ANY(%s) AND status='pending_approval'
                   GROUP BY vault_id, group_id""", (job_ids,),
            ).fetchone()
            if not job:
                return
            params = {"count": int(job["count"]), "cost": f"{float(job['cost'] or 0):.2f}",
                      "path": job["path"], "group_id": job["group_id"]}
            channels = ("in_app", "email") if _smtp_config() is not None else ("in_app",)
            for recipient in _owner_recipients(connection):
                enqueue_notification(
                    connection, user_id=int(recipient["user_id"]),
                    vault_id=job["vault_id"], event="recovery_approval", title="",
                    title_key="notification.recovery_approval.title",
                    body_key="notification.recovery_approval.body", message_params=params,
                    channels=channels,
                    dedupe_key=f"recovery:{job['vault_id']}:{job['group_id']}:approval",
                )
    except Exception:
        _logger.warning("Recovery approval notification failed; recovery request remains queued for approval")


def enqueue_job_terminal_notification(connection: Any, *, job_id: int) -> int:
    """Notify the Owner of terminal Jobs; failures also use optional SMTP."""
    job = connection.execute(
        """
        SELECT id, vault_id, path, action, status, origin
        FROM jobs WHERE id=%s
        """,
        (job_id,),
    ).fetchone()
    if not job or job["status"] not in {"completed", "failed"}:
        return 0
    vault_id = int(job["vault_id"])
    recovery = job["action"] == "recover" and job["origin"] != "automatic"
    if not recovery and not (job["action"] == "upload" and job["status"] == "failed"):
        return 0
    event = ("recovery_completed" if job["status"] == "completed" else "recovery_failed") if recovery else "job_failed"
    if event == "job_failed" and _fold_into_failure_group(connection, job):
        return 0
    title_key = f"notification.{event}.title"
    body_key = f"notification.{event}.body"
    params = {"action": str(job["action"]), "path": str(job["path"])}
    recipients = _owner_recipients(connection)
    smtp_enabled = _smtp_config() is not None

    enqueued = 0
    for recipient in recipients:
        user_id = int(recipient["user_id"])
        channels = ["in_app"]
        if smtp_enabled:
            channels.append("email")
        dedupe_key = f"job:{int(job['id'])}:{event}"
        existing = connection.execute(
            """
            SELECT id FROM notifications
            WHERE user_id=%s AND dedupe_key=%s
            """,
            (user_id, dedupe_key),
        ).fetchone()
        enqueue_notification(
            connection,
            user_id=user_id,
            vault_id=vault_id,
            job_id=int(job["id"]),
            event=event,
            title="",
            body="",
            title_key=title_key,
            body_key=body_key,
            message_params=params,
            dedupe_key=dedupe_key,
            channels=tuple(channels),
        )
        if event == "job_failed":
            # Late outbox delivery must materialize already-resolved history,
            # never reopen an exact target completed by remediation.
            from .upload_remediation import CLOSED_SQL as RESOLUTION_SQL
            connection.execute(
                f"""UPDATE notifications n SET
                    problem_status=CASE WHEN {RESOLUTION_SQL} THEN 'resolved'
                        ELSE COALESCE(n.problem_status,'open') END,
                    resolved_at=CASE WHEN {RESOLUTION_SQL} THEN COALESCE(n.resolved_at,%s)
                        ELSE n.resolved_at END,
                    read_at=CASE WHEN {RESOLUTION_SQL} THEN COALESCE(n.read_at,%s)
                        ELSE n.read_at END
                    FROM jobs original WHERE original.id=n.job_id
                      AND n.user_id=%s AND n.dedupe_key=%s""",
                (now_iso(), now_iso(), user_id, dedupe_key),
            )
        if existing is None:
            enqueued += 1
    return enqueued


def _fold_into_failure_group(connection: Any, job: Mapping[str, Any]) -> bool:
    """Collapse a failure burst into one per-Vault problem instead of N rows.

    A burst is more than GROUP_THRESHOLD failed uploads of one Vault inside the
    last GROUP_WINDOW_SECONDS. The first GROUP_THRESHOLD keep their own rows;
    later ones are counted by one ``job_failed_group`` problem, and only while
    the burst rate holds. When a failure arrives and the Vault is no longer in a
    burst, the group is retired (its key is freed; it stays open until its own
    failures, between its ``since`` and ``last``, are resolved or moot) and the
    failure gets a notification of its own. A failure that arrives after the
    group went quiet for a whole window never joins it: a new burst starts a new
    group. A Job that already has its own row is never folded (re-delivery
    stays idempotent).
    """
    recipients = _owner_recipients(connection)
    if not recipients:
        return False
    vault_id = int(job["vault_id"])
    user_id = int(recipients[0]["user_id"])
    own = connection.execute(
        "SELECT 1 FROM notifications WHERE user_id=%s AND dedupe_key=%s",
        (user_id, f"job:{int(job['id'])}:job_failed"),
    ).fetchone()
    if own is not None:
        return False
    stamp = now_iso()
    cutoff = (datetime.fromisoformat(stamp) - timedelta(seconds=GROUP_WINDOW_SECONDS)).isoformat()
    group_key = f"job_failed_group:{vault_id}"
    burst = connection.execute(
        """
        SELECT count(*) AS total FROM jobs
        WHERE vault_id=%s AND action='upload' AND status='failed'
          AND updated_at >= %s
        """,
        (vault_id, cutoff),
    ).fetchone()
    in_burst = int(burst["total"] or 0) > GROUP_THRESHOLD
    previous = connection.execute(
        "SELECT * FROM notifications WHERE user_id=%s AND dedupe_key=%s",
        (user_id, group_key),
    ).fetchone()
    since = cutoff
    if previous is not None:
        old = parse_message_params(previous.get("message_params"))
        last_folded = str(old.get("last") or old.get("since") or "")
        if in_burst and previous.get("problem_status") == "open" and last_folded >= cutoff:
            since = str(old.get("since") or cutoff)
        else:
            # The burst is over: retire the group so it cannot absorb this failure.
            connection.execute(
                "UPDATE notifications SET dedupe_key=%s WHERE id=%s",
                (f"{group_key}:{previous['id']}", previous["id"]),
            )
            previous = None
    if not in_burst:
        return False
    if previous is None:
        recent = connection.execute(
            """
            SELECT count(*) AS total FROM notifications
            WHERE user_id=%s AND vault_id=%s AND event='job_failed'
              AND problem_status='open' AND created_at >= %s
            """,
            (user_id, vault_id, cutoff),
        ).fetchone()
        if int(recent["total"] or 0) < GROUP_THRESHOLD:
            return False
        # A new group never re-counts failures a retired group already holds.
        retired = connection.execute(
            """
            SELECT message_params FROM notifications
            WHERE user_id=%s AND vault_id=%s AND event='job_failed_group'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, vault_id),
        ).fetchone()
        if retired is not None:
            retired_last = str(parse_message_params(retired.get("message_params")).get("last") or "")
            if retired_last > since:
                since = retired_last
    count = connection.execute(
        """
        SELECT count(*) AS total FROM jobs original
        WHERE original.vault_id=%s AND original.action='upload'
          AND original.status='failed' AND original.updated_at >= %s
          AND NOT EXISTS (SELECT 1 FROM notifications own
                          WHERE own.job_id=original.id AND own.event='job_failed')
        """,
        (vault_id, since),
    ).fetchone()
    params = {"count": int(count["total"] or 1), "since": since, "last": stamp}
    item = enqueue_notification(
        connection, user_id=user_id, vault_id=vault_id, event="job_failed_group",
        title="", title_key="notification.job_failed_group.title",
        body_key="notification.job_failed_group.body", message_params=params,
        dedupe_key=group_key, channels=fixed_channels(),
    )
    connection.execute(
        "UPDATE notifications SET problem_status='open', resolved_at=NULL, "
        "message_params=%s, body=%s WHERE id=%s",
        (format_message_params(params), translate("notification.job_failed_group.body", **params),
         item["id"]),
    )
    return True


def enqueue_archive_version_missing(
    connection: Any, *, incident: Mapping[str, Any]
) -> int:
    """Notify the Owner of unexpected loss."""
    return _enqueue_incident_notification(
        connection,
        incident=incident,
        event="archive_version_missing",
        params={
            "path": str(incident.get("path") or ""),
            "archive_version_id": str(incident.get("archive_version_id") or ""),
            "provider_version_id": str(incident.get("provider_version_id") or ""),
            "observed_at": str(incident.get("opened_at") or now_iso()),
            "reason": str(incident.get("reason") or "missing_in_cloud"),
        },
    )


def enqueue_cloud_only_conflict(
    connection: Any, *, incident: Mapping[str, Any]
) -> int:
    """Tell the owner that new content is held back at a cloud-only path."""
    return _enqueue_incident_notification(
        connection,
        incident=incident,
        event="cloud_only_conflict",
        params={
            "path": str(incident.get("path") or ""),
            "observed_at": str(incident.get("opened_at") or now_iso()),
        },
    )


def _enqueue_incident_notification(
    connection: Any,
    *,
    incident: Mapping[str, Any],
    event: str,
    params: Mapping[str, Any],
) -> int:
    """Notify the owner once per catalog incident."""
    vault_id = int(incident["vault_id"])
    title_key = f"notification.{event}.title"
    body_key = f"notification.{event}.body"
    recipients = _owner_recipients(connection)
    smtp_enabled = _smtp_config() is not None
    enqueued = 0
    for recipient in recipients:
        user_id = int(recipient["user_id"])
        channels = ["in_app"]
        if smtp_enabled:
            channels.append("email")
        dedupe_key = f"incident:{int(incident['id'])}"
        existing = connection.execute(
            """
            SELECT id FROM notifications
            WHERE user_id=%s AND dedupe_key=%s
            """,
            (user_id, dedupe_key),
        ).fetchone()
        enqueue_notification(
            connection,
            user_id=user_id,
            vault_id=vault_id,
            event=event,
            title="",
            body="",
            title_key=title_key,
            body_key=body_key,
            message_params=params,
            dedupe_key=dedupe_key,
            channels=tuple(channels),
        )
        connection.execute(
            "UPDATE notifications SET problem_status=COALESCE(problem_status, 'open') "
            "WHERE user_id=%s AND dedupe_key=%s",
            (user_id, dedupe_key),
        )
        if existing is None:
            enqueued += 1
    return enqueued


def enqueue_job_terminal_notification_best_effort(
    connection: Any, *, job_id: int
) -> int | None:
    """Enqueue a terminal notification without weakening the Job transition.

    The caller's transaction owns the authoritative Job update.  A failed
    notification insert can put a PostgreSQL transaction into the failed state
    (and can leave partial rows on either backend), so isolate the best-effort
    work in a real savepoint rather than merely catching the exception.
    Returns the enqueue count on success, or ``None`` when the savepoint
    rolled back so a later reconciliation can retry.
    """
    savepoint = "job_terminal_notification"
    try:
        connection.execute(f"SAVEPOINT {savepoint}")
    except Exception:
        # A PostgreSQL transaction always supports SAVEPOINT. If
        # a connection wrapper does not, skip optional notification work rather
        # than making the already-authoritative Job transition fail.
        return None

    try:
        enqueued = enqueue_job_terminal_notification(connection, job_id=job_id)
    except Exception as exc:
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        finally:
            # RELEASE is required after ROLLBACK TO so the outer transaction
            # remains usable.
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        # Deadlock/serialization victims must replay the authoritative DB unit,
        # not commit a partial outcome merely because delivery is best-effort.
        from .database_retry import is_retryable_transaction_error
        if is_retryable_transaction_error(exc):
            raise
        return None

    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    mark_terminal_notification_done(connection, job_id=job_id)
    return enqueued


def mark_terminal_notification_done(connection: Any, *, job_id: int) -> bool:
    result = connection.execute(
        """
        UPDATE jobs
        SET terminal_notification_status='done'
        WHERE id=%s AND status IN ('completed', 'failed')
        """,
        (job_id,),
    )
    return bool(getattr(result, "rowcount", 0))


def pending_terminal_notification_count(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM jobs
        WHERE status IN ('completed', 'failed')
          AND terminal_notification_status='pending'
        """
    ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def reconcile_pending_terminal_notifications(
    connection: Any, *, limit: int = 100
) -> int:
    """Retry terminal notification materialization left pending after a fault."""
    rows = connection.execute(
        """
        SELECT id FROM jobs
        WHERE status IN ('completed', 'failed')
          AND terminal_notification_status='pending'
        ORDER BY updated_at ASC, id ASC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    recovered = 0
    for row in rows:
        result = enqueue_job_terminal_notification_best_effort(
            connection, job_id=int(row["id"])
        )
        if result is None:
            continue
        mark_terminal_notification_done(connection, job_id=int(row["id"]))
        recovered += 1
    return recovered


def fixed_channels() -> tuple[str, ...]:
    return ("in_app", "email") if _smtp_config() else ("in_app",)


def email_configured() -> bool:
    """Public status only; never expose addresses or transport credentials."""
    return _smtp_config() is not None


def set_problem(
    connection: Any, *, key: str, event: str, vault_id: int | None = None,
    active: bool, params: Mapping[str, Any] | None = None,
    email: bool = True,
) -> dict[str, Any] | None:
    """One current row per problem; notify once per open episode, not per poll.

    Taking the Owner lock serializes reopening and delivery creation. Reading
    an open problem never resolves it or re-arms email. A recurrence does both.
    Only safe structured context belongs in params, never exception strings.
    """
    recipients = _owner_recipients(connection)
    if not recipients:
        return None
    owner_id = int(recipients[0]["user_id"])
    # Serialize episodes, but remain compatible with FK KEY SHARE acquired by
    # concurrent Job/notification inserts while they hold a Vault lock.
    connection.execute("SELECT id FROM users WHERE id=%s FOR NO KEY UPDATE", (owner_id,))
    previous = connection.execute(
        "SELECT * FROM notifications WHERE user_id=%s AND dedupe_key=%s",
        (owner_id, key),
    ).fetchone()
    stamp = now_iso()
    if not active:
        if previous and previous.get("problem_status") == "open":
            connection.execute(
                "UPDATE notifications SET problem_status='resolved', resolved_at=%s, "
                "read_at=COALESCE(read_at,%s) WHERE id=%s", (stamp, stamp, previous["id"]),
            )
        return None
    context = dict(params or {})
    item = enqueue_notification(
        connection, user_id=owner_id, vault_id=vault_id, event=event,
        title="", title_key=f"notification.{event}.title",
        body_key=f"notification.{event}.body", message_params=context,
        dedupe_key=key, channels=fixed_channels() if email else ("in_app",),
    )
    reopen = previous and previous.get("problem_status") == "resolved"
    connection.execute(
        "UPDATE notifications SET problem_status='open', resolved_at=NULL, "
        "message_params=%s, body=%s, read_at=CASE WHEN %s THEN NULL ELSE read_at END, "
        "created_at=CASE WHEN %s THEN %s ELSE created_at END WHERE id=%s",
        (format_message_params(context), translate(f"notification.{event}.body", **context),
         bool(reopen), bool(reopen), stamp, item["id"]),
    )
    if reopen:
        connection.execute(
            "UPDATE notification_deliveries SET status='pending', attempt_count=0, "
            "next_attempt_at=%s, last_error=NULL, updated_at=%s WHERE notification_id=%s",
            (stamp, stamp, item["id"]),
        )
    return _row_notification(connection.execute("SELECT * FROM notifications WHERE id=%s", (item["id"],)).fetchone())


def reconcile_problem_states(connection: Any) -> None:
    """Refresh catalog/Job-backed problems before counting, listing or emailing.

    No filesystem/S3 operations and no modification of protection evidence.
    Only demonstrable completed remediation with exact cleanup resolves failed
    uploads. A newer queued/failed upload, or successful content B, proves nothing
    about failed snapshot A. Reading remains independent of resolution.
    """
    from .upload_remediation import CLOSED_SQL as RESOLUTION_SQL
    connection.execute(f"""
        UPDATE notifications n SET problem_status='resolved', resolved_at=%s,
            read_at=COALESCE(read_at,%s)
        WHERE n.problem_status='open' AND (
            (n.event IN ('archive_version_missing', 'cloud_only_conflict') AND EXISTS (
                SELECT 1 FROM catalog_incidents i
                WHERE n.dedupe_key='incident:' || i.id::text AND i.status='resolved'
            )) OR (n.event='job_failed' AND EXISTS (
                SELECT 1 FROM jobs original WHERE original.id=n.job_id
                  AND ({RESOLUTION_SQL} OR {REMOVED_FILE_FAILURE_SQL})
            )) OR (n.event='job_failed_group' AND NOT EXISTS (
                SELECT 1 FROM jobs original
                WHERE original.vault_id=n.vault_id AND original.action='upload'
                  AND original.status='failed'
                  AND original.updated_at >= (n.message_params::jsonb->>'since')
                  AND original.updated_at <= COALESCE(n.message_params::jsonb->>'last', '9999-12-31')
                  AND NOT EXISTS (SELECT 1 FROM notifications own
                                  WHERE own.job_id=original.id AND own.event='job_failed')
                  AND NOT ({RESOLUTION_SQL} OR {REMOVED_FILE_FAILURE_SQL})
            )) OR (n.event='file_unreadable' AND (
                EXISTS (SELECT 1 FROM vault_component_health h WHERE h.vault_id=n.vault_id
                        AND h.component='source' AND h.last_error IS NULL AND h.last_success_at>n.created_at)
                OR EXISTS (SELECT 1 FROM file_paths fp JOIN local_copies lc ON lc.vault_file_id=fp.vault_file_id
                           WHERE fp.vault_id=n.vault_id AND fp.valid_to IS NULL
                             AND fp.path=(n.message_params::jsonb->>'path') AND lc.observed_at>n.created_at)
            ))
        )
    """, (now_iso(), now_iso()))


def count_open_problems(connection: Any, *, user_id: int) -> int:
    reconcile_problem_states(connection)
    row = connection.execute(
        "SELECT count(*) AS total FROM notifications WHERE user_id=%s "
        "AND COALESCE(in_app_enabled,TRUE)=TRUE AND problem_status='open'", (user_id,),
    ).fetchone()
    return int(row["total"] or 0)


def _visible_notification_predicate(alias: str = "n") -> str:
    """Return the inbox visibility predicate for a SELECT or UPDATE.

    SELECT callers pass the explicit ``n`` alias.  UPDATE deliberately passes
    an empty alias to qualify the target table's columns directly.
    """
    prefix = f"{alias}." if alias else ""
    return f"""
        {prefix}user_id=%s
        AND COALESCE({prefix}in_app_enabled, TRUE)=TRUE
    """


def _normalize_notification_status(status: str | None) -> str:
    """Accept unread/read/all filters for the in-app inbox (issue #225)."""
    normalized = (status or "all").strip().lower()
    if normalized not in {"all", "unread", "read"}:
        raise ValueError("status must be unread, read, or all")
    return normalized


# Public HTTP pages are capped at 200. Callers may request one extra row
# (201) as a has_more sentinel without clamping the probe away.
_MAX_NOTIFICATION_LIST_FETCH = 201


def list_in_app_notifications(
    connection: Any,
    *,
    user_id: int,
    limit: int = 50,
    locale: str | None = None,
    status: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """List visible in-app notifications, optionally filtered and cursor-paged.

    ``status`` selects unread, read, or the mixed newest-first list (``all``).
    ``before_id`` returns strictly older rows (lower id) for incremental history.
    ``limit`` accepts up to 201 so HTTP handlers can probe ``has_more`` at the
    public maximum page size of 200.
    """
    reconcile_problem_states(connection)
    clauses = [_visible_notification_predicate()]
    params: list[Any] = [user_id]

    normalized_status = _normalize_notification_status(status)
    if normalized_status == "unread":
        clauses.append("n.read_at IS NULL")
    elif normalized_status == "read":
        clauses.append("n.read_at IS NOT NULL")

    if before_id is not None:
        before = int(before_id)
        if before < 1:
            raise ValueError("before_id must be a positive integer")
        clauses.append("n.id < %s")
        params.append(before)

    params.append(max(1, min(int(limit), _MAX_NOTIFICATION_LIST_FETCH)))
    rows = connection.execute(
        f"""
        SELECT n.* FROM notifications n
        WHERE {' AND '.join(clauses)}
        ORDER BY n.id DESC
        LIMIT %s
        """,
        tuple(params),
    ).fetchall()
    return [_row_notification(row, locale=locale) for row in rows]


def count_unread_notifications(connection: Any, *, user_id: int) -> int:
    reconcile_problem_states(connection)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM notifications n
        WHERE {_visible_notification_predicate()}
          AND n.read_at IS NULL
        """,
        (user_id,),
    ).fetchone()
    return int(row["total"] or 0) if row else 0


def mark_notification_read(
    connection: Any,
    *,
    notification_id: int,
    user_id: int,
    locale: str | None = None,
) -> dict[str, Any] | None:
    reconcile_problem_states(connection)
    stamp = now_iso()
    update_predicate = _visible_notification_predicate("")
    connection.execute(
        f"""
        UPDATE notifications
        SET read_at=COALESCE(read_at, %s)
        WHERE id=%s AND {update_predicate}
        """,
        (stamp, notification_id, user_id),
    )
    select_predicate = _visible_notification_predicate("n")
    row = connection.execute(
        f"""
        SELECT n.* FROM notifications n
        WHERE n.id=%s AND {select_predicate}
        """,
        (notification_id, user_id),
    ).fetchone()
    return _row_notification(row, locale=locale) if row else None


def mark_all_notifications_read(
    connection: Any,
    *,
    user_id: int,
) -> dict[str, int]:
    """Mark every currently visible unread notification read (issue #225).

    Idempotent: already-read rows and notifications outside the Vault/
    in-app visibility predicate are left untouched. Returns the number of
    rows newly stamped plus the authoritative remaining unread count.
    """
    reconcile_problem_states(connection)
    stamp = now_iso()
    update_predicate = _visible_notification_predicate("")
    result = connection.execute(
        f"""
        UPDATE notifications
        SET read_at=COALESCE(read_at, %s)
        WHERE {update_predicate}
          AND read_at IS NULL
        """,
        (stamp, user_id),
    )
    marked_count = int(getattr(result, "rowcount", 0) or 0)
    return {
        "marked_count": marked_count,
        "unread_count": count_unread_notifications(connection, user_id=user_id),
    }


def _smtp_config() -> dict[str, Any] | None:
    """Resolve the optional recipient and transport only from the environment."""
    if not all(
        value.strip()
        for value in (
            settings.smtp_host,
            settings.smtp_from_address,
            settings.smtp_to_address,
        )
    ):
        return None
    return {
        "host": settings.smtp_host.strip(),
        "port": settings.smtp_port,
        "username": settings.smtp_username,
        "password": settings.smtp_password,
        "from_address": settings.smtp_from_address.strip(),
        "to_address": settings.smtp_to_address.strip(),
        "use_tls": settings.smtp_use_tls,
    }


def deliver_pending_notifications(
    connection: Any,
    *,
    smtp_client: SmtpClient | None = None,
    max_attempts: int = 3,
) -> dict[str, int]:
    """Attempt pending email deliveries without touching Job rows or the inbox."""
    reconcile_problem_states(connection)
    stamp = now_iso()
    pending = connection.execute(
        """
        SELECT d.*, n.event AS n_event, n.title AS n_title, n.body AS n_body,
               n.user_id AS n_user_id, n.vault_id AS n_vault_id, n.job_id AS n_job_id,
               n.message_params AS n_message_params
        FROM notification_deliveries d
        JOIN notifications n ON n.id = d.notification_id
        WHERE d.status = 'pending'
          AND (n.problem_status IS NULL OR n.problem_status='open')
          AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= %s)
        ORDER BY d.id
        """,
        (stamp,),
    ).fetchall()

    stats = {"attempted": 0, "delivered": 0, "failed": 0, "requeued": 0}
    smtp_config = _smtp_config()

    for row in pending:
        stats["attempted"] += 1
        attempt = int(row["attempt_count"]) + 1
        error: str | None = None
        try:
            if row["channel"] == "email":
                if smtp_client is None or smtp_config is None:
                    raise RuntimeError("smtp endpoint unavailable")
                from .email_templates import render_email

                rendered = render_email(
                    row["n_event"],
                    title=row["n_title"],
                    body=row["n_body"],
                    vault_id=row["n_vault_id"],
                )
                from .notification_links import notification_url
                params = _message_params({"message_params": row.get("n_message_params")})
                link = notification_url(getattr(settings, "app_public_url", ""), row["n_vault_id"], params)
                if link:
                    rendered["body"] += "\n" + link + "\n"
                to_address = smtp_config["to_address"]
                smtp_client.send(
                    {
                        "template": row["n_event"],
                        "subject": rendered["subject"],
                        "body": rendered["body"],
                        "user_id": row["n_user_id"],
                        "from_address": smtp_config.get("from_address"),
                        "to_address": to_address,
                        "host": smtp_config.get("host"),
                        "port": smtp_config.get("port") or 587,
                        "username": smtp_config.get("username") or "",
                        "password": smtp_config.get("password") or "",
                        "use_tls": smtp_config.get("use_tls", True),
                    }
                )
            else:
                raise RuntimeError(f"unsupported channel: {row['channel']}")
        except Exception as exc:  # system boundary failure
            # Injectable adapters are also untrusted boundaries.
            error = "smtp delivery failed"

        if error is None:
            connection.execute(
                """
                UPDATE notification_deliveries
                SET status='delivered', attempt_count=%s, last_error=NULL,
                    next_attempt_at=NULL, updated_at=%s
                WHERE id=%s
                """,
                (attempt, stamp, row["id"]),
            )
            stats["delivered"] += 1
            continue

        if attempt >= max_attempts:
            connection.execute(
                """
                UPDATE notification_deliveries
                SET status='failed', attempt_count=%s, last_error=%s,
                    next_attempt_at=NULL, updated_at=%s
                WHERE id=%s
                """,
                (attempt, error[:500], stamp, row["id"]),
            )
            stats["failed"] += 1
        else:
            retry_at = _delivery_retry_at(attempt, now=stamp)
            connection.execute(
                """
                UPDATE notification_deliveries
                SET status='pending', attempt_count=%s, last_error=%s,
                    next_attempt_at=%s, updated_at=%s
                WHERE id=%s
                """,
                (attempt, error[:500], retry_at, stamp, row["id"]),
            )
            stats["requeued"] += 1
    return stats


def _delivery_retry_at(attempt: int, *, now: str | None = None) -> str:
    """Exponential backoff that stays due after ``now`` for the next worker poll."""
    delay = min(DELIVERY_RETRY_CAP_SECONDS, 2 ** max(1, int(attempt)))
    if now:
        try:
            current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except ValueError:
            current = datetime.now(timezone.utc)
    else:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current + timedelta(seconds=delay)).replace(microsecond=0).isoformat()


def production_smtp_client() -> SmtpClient | None:
    """Build the SMTP adapter only when deployment email is configured."""
    return SmtpNotificationClient() if _smtp_config() else None


class SmtpNotificationClient:
    """Send one notification through the deployment-owned SMTP endpoint."""

    timeout = int(DELIVERY_TIMEOUT_SECONDS)

    def send(self, message: dict[str, Any]) -> None:
        host = str(message.get("host") or "").strip()
        port = int(message.get("port") or 587)
        username = str(message.get("username") or "")
        password = str(message.get("password") or "")
        from_address = str(message.get("from_address") or "").strip()
        to_address = str(message.get("to_address") or "").strip()
        use_tls = bool(message.get("use_tls", True))
        if not host or not from_address or not to_address:
            raise RuntimeError("smtp endpoint unavailable")
        email = EmailMessage()
        email["From"] = from_address
        email["To"] = to_address
        email["Subject"] = str(message.get("subject") or "")
        email.set_content(str(message.get("body") or ""))
        try:
            if use_tls and port == 465:
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                    host,
                    port,
                    timeout=self.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                smtp = smtplib.SMTP(host, port, timeout=self.timeout)
            with smtp:
                smtp.ehlo()
                if use_tls and port != 465:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(email)
        except Exception as exc:
            _logger.warning("smtp delivery failed: %s", exc.__class__.__name__)
            raise RuntimeError("smtp delivery failed") from exc
