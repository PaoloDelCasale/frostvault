"""Owner notifications with fixed routing and optional deployment-owned SMTP.

The inbox belongs to the single Owner: notifications have no recipient and
are deduplicated by a unique ``dedupe_key``. Delivery retries are bounded and
never mutate the Job that triggered the notification. Problems have a current
open/resolved state independent of reading.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from ..config import settings
from ..i18n import catalog as locale_catalog
from ..i18n import normalize_locale, parse_message_params, translate


# Upload failures beyond GROUP_THRESHOLD per Vault inside GROUP_WINDOW_SECONDS fold
# into one open "job_failed_group" problem instead of one row per file (#499).
GROUP_THRESHOLD = 10
GROUP_WINDOW_SECONDS = 600

DELIVERY_TIMEOUT_SECONDS = 10.0
DELIVERY_RETRY_CAP_SECONDS = 300
_logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _now() -> datetime:
    """The notification clock as a timestamp (``now_iso`` stays the one seam)."""
    return datetime.fromisoformat(now_iso())


def _params_json(params: Mapping[str, Any] | None) -> Jsonb:
    """Message params as a JSON object; non-JSON values are stored as text."""
    return Jsonb(json.loads(json.dumps(dict(params or {}), ensure_ascii=False, default=str)))


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


def _recovery_approval_params(params: Mapping[str, Any], locale: str | None = None) -> dict[str, Any]:
    """Keep the stored decimal evidence; apply #530's sub-cent rule at render."""
    localized = dict(params)
    try:
        amount = Decimal(str(params.get("cost") or 0))
    except InvalidOperation:
        amount = Decimal("NaN")
    if not amount.is_finite():
        localized["cost"] = "—"
        return localized
    subcent = 0 < amount < Decimal("0.01")
    shown = Decimal("0.01") if subcent else amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    digits = f"{shown:,.2f}"
    if normalize_locale(locale) == "it":
        digits = digits.translate(str.maketrans({",": ".", ".": ","}))
        money = f"{digits} €"
    else:
        money = f"€{digits}"
    localized["cost"] = f"< {money}" if subcent else money
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
    params = _localized_params(_message_params(row), locale)
    if row.get("event") == "recovery_approval":
        params = _recovery_approval_params(params, locale)
    return translate(key, locale=locale, **params)


def render_weekly_summary(summary: Mapping[str, Any], *, locale: str | None = None) -> str:
    """Render the same structured summary in either supported language."""
    states = summary.get("states") or {}
    counts = " · ".join(f"{translate('state.' + state, locale=locale)}: {count}"
                        for state, count in states.items())
    verification = summary.get("metadata_backup_verification") or {}
    verified = verification.get("last_verified")
    backup = (translate("notification.weekly_summary.backup_run", locale=locale,
                        id=verified["id"], created=verified["created_at"],
                        verified=verified["verified_at"])
              if verified else summary.get("last_verified_backup") or "—")
    lines = [counts, translate("notification.weekly_summary.metrics", locale=locale,
             backup=backup, problems=summary.get("open_problems", 0),
             files=summary.get("freed_files", 0), gib=f"{int(summary.get('freed_bytes', 0)) / 1024**3:.2f}",
             cost=f"{summary.get('monthly_cost_estimate', 0):.2f}", currency=summary.get("currency", "EUR"),
             blocked=summary.get("cleanup_blocked", 0))]
    latest = verification.get("latest")
    if latest and (not verified or latest["id"] != verified["id"]):
        # A newer backup than the verified one says when its turn comes (#648).
        expected = (translate("notification.weekly_summary.verification_expected", locale=locale,
                              at=verification["next_verification_at"])
                    if verification.get("next_verification_at")
                    else translate("notification.weekly_summary.verification_disabled", locale=locale))
        lines.append(translate("notification.weekly_summary.latest_backup", locale=locale,
                               id=latest["id"], created=latest["created_at"], expected=expected))
    for vault in summary.get("vaults") or []:
        lines.append(translate("notification.weekly_summary.vault", locale=locale,
                     vault=vault["vault_id"], scan=vault.get("last_scan") or "—",
                     audit=vault.get("last_audit") or "—", suspended=vault["suspended"]))
    for problem in summary.get("problems") or []:
        lines.append(translate(problem["body_key"], locale=locale,
                               **_localized_params(_message_params(problem), locale))
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


def _lock_owner_inbox(connection: Any) -> None:
    """Serialize problem episodes and failure groups on the single Owner row."""
    connection.execute("SELECT id FROM owner WHERE id=1 FOR NO KEY UPDATE")


def _ensure_delivery(
    connection: Any, *, notification_id: int, channel: str, stamp: datetime
) -> None:
    if channel == "in_app":
        return
    if channel != "email":
        raise ValueError(f"invalid delivery channel: {channel}")
    connection.execute(
        """
        INSERT INTO notification_deliveries(
            notification_id, channel, status, attempt_count,
            next_attempt_at, updated_at
        ) VALUES (%s, %s, 'pending', 0, %s, %s)
        ON CONFLICT (notification_id, channel) DO NOTHING
        """,
        (notification_id, channel, stamp, stamp),
    )


def enqueue_notification(
    connection: Any,
    *,
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
    """Persist one Owner notification and attach any requested outbound channels.

    ``in_app_enabled`` is false for email-only notices. When ``dedupe_key`` is
    set, an existing notification with that key is returned instead of a new
    one (it is only made visible in-app when requested).
    """
    requested_channels = tuple(dict.fromkeys(channels))
    for channel in requested_channels:
        if channel not in {"in_app", "email"}:
            raise ValueError(f"invalid delivery channel: {channel}")
    params = dict(message_params or {})
    if title_key and not title:
        title = translate(title_key, **params)
    if body_key and not body:
        rendered_params = _recovery_approval_params(params) if event == "recovery_approval" else params
        body = translate(body_key, **rendered_params)
    effective_in_app = (
        bool(in_app_enabled)
        if in_app_enabled is not None
        else "in_app" in requested_channels
    )
    stamp = _now()
    if event == "weekly_summary":
        body = render_weekly_summary(params)

    row = connection.execute(
        """
        INSERT INTO notifications(
            vault_id, job_id, event, title, body,
            title_key, body_key, message_params, in_app_enabled,
            dedupe_key, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING *
        """,
        (
            vault_id,
            job_id,
            event,
            title,
            body,
            title_key,
            body_key,
            _params_json(params),
            effective_in_app,
            dedupe_key,
            stamp,
        ),
    ).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT * FROM notifications WHERE dedupe_key=%s", (dedupe_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Notification deduplication lookup failed")
        if effective_in_app and not row["in_app_enabled"]:
            row = connection.execute(
                "UPDATE notifications SET in_app_enabled=TRUE WHERE id=%s RETURNING *",
                (row["id"],),
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
                """SELECT j.vault_id, j.group_id, min(f.path) AS path, count(*) AS count,
                          max(r.estimated_cost_eur) AS cost
                   FROM jobs j JOIN files f ON f.id = j.file_id
                   LEFT JOIN restore_requests r ON r.group_id = j.group_id
                   WHERE j.id=ANY(%s) AND j.status='pending_approval'
                   GROUP BY j.vault_id, j.group_id""", (job_ids,),
            ).fetchone()
            if not job:
                return
            params = {"count": int(job["count"]), "cost": str(job["cost"] or 0),
                      "path": job["path"], "group_id": job["group_id"]}
            channels = ("in_app", "email") if _smtp_config() is not None else ("in_app",)
            enqueue_notification(
                connection,
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
        SELECT j.id, j.vault_id, f.path, j.action, j.status, j.origin
        FROM jobs j JOIN files f ON f.id = j.file_id
        WHERE j.id=%s
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
    from .upload_failure_problems import CLOSED_UPLOAD_FAILURE_SQL as RESOLUTION_SQL
    already_closed = event == "job_failed" and connection.execute(
        f"SELECT 1 FROM jobs original WHERE original.id=%s AND {RESOLUTION_SQL}", (job_id,),
    ).fetchone() is not None
    if event == "job_failed" and not already_closed and _fold_into_failure_group(connection, job):
        return 0
    title_key = f"notification.{event}.title"
    body_key = f"notification.{event}.body"
    params = {"action": str(job["action"]), "path": str(job["path"])}
    channels = ("in_app", "email") if _smtp_config() is not None else ("in_app",)
    dedupe_key = f"job:{int(job['id'])}:{event}"
    existing = connection.execute(
        "SELECT id FROM notifications WHERE dedupe_key=%s", (dedupe_key,),
    ).fetchone()
    enqueue_notification(
        connection,
        vault_id=vault_id,
        job_id=int(job["id"]),
        event=event,
        title="",
        body="",
        title_key=title_key,
        body_key=body_key,
        message_params=params,
        dedupe_key=dedupe_key,
        channels=channels,
    )
    if event == "job_failed":
        # Late outbox delivery must materialize already-resolved history,
        # never reopen an exact target completed by remediation.
        stamp = _now()
        connection.execute(
            f"""UPDATE notifications n SET
                problem_status=CASE WHEN %s OR {RESOLUTION_SQL} THEN 'resolved'
                    ELSE COALESCE(n.problem_status,'open') END,
                resolved_at=CASE WHEN %s OR {RESOLUTION_SQL} THEN COALESCE(n.resolved_at,%s)
                    ELSE n.resolved_at END,
                read_at=CASE WHEN %s OR {RESOLUTION_SQL} THEN COALESCE(n.read_at,%s)
                    ELSE n.read_at END
                FROM jobs original WHERE original.id=n.job_id AND n.dedupe_key=%s""",
            (already_closed, already_closed, stamp, already_closed, stamp, dedupe_key),
        )
    return 0 if existing is not None else 1


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
    vault_id = int(job["vault_id"])
    from .upload_failure_problems import CLOSED_UPLOAD_FAILURE_SQL
    own = connection.execute(
        "SELECT 1 FROM notifications WHERE dedupe_key=%s",
        (f"job:{int(job['id'])}:job_failed",),
    ).fetchone()
    if own is not None:
        return False
    now = _now()
    stamp = now.isoformat()
    cutoff = (now - timedelta(seconds=GROUP_WINDOW_SECONDS)).isoformat()
    group_key = f"job_failed_group:{vault_id}"
    burst = connection.execute(
        f"""
        SELECT count(*) AS total FROM jobs original
        WHERE original.vault_id=%s AND original.action='upload' AND original.status='failed'
          AND original.updated_at >= %s AND NOT {CLOSED_UPLOAD_FAILURE_SQL}
        """,
        (vault_id, cutoff),
    ).fetchone()
    in_burst = int(burst["total"] or 0) > GROUP_THRESHOLD
    previous = connection.execute(
        "SELECT * FROM notifications WHERE dedupe_key=%s", (group_key,),
    ).fetchone()
    since = cutoff
    if previous is not None:
        old = _message_params(previous)
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
            WHERE vault_id=%s AND event='job_failed'
              AND problem_status='open' AND created_at >= %s
            """,
            (vault_id, cutoff),
        ).fetchone()
        if int(recent["total"] or 0) < GROUP_THRESHOLD:
            return False
        # A new group never re-counts failures a retired group already holds.
        retired = connection.execute(
            """
            SELECT message_params FROM notifications
            WHERE vault_id=%s AND event='job_failed_group'
            ORDER BY id DESC LIMIT 1
            """,
            (vault_id,),
        ).fetchone()
        if retired is not None:
            retired_last = str(_message_params(retired).get("last") or "")
            if retired_last > since:
                since = retired_last
    count = connection.execute(
        f"""
        SELECT count(*) AS total FROM jobs original
        WHERE original.vault_id=%s AND original.action='upload'
          AND original.status='failed' AND original.updated_at >= %s
          AND NOT {CLOSED_UPLOAD_FAILURE_SQL}
          AND NOT EXISTS (SELECT 1 FROM notifications own
                          WHERE own.job_id=original.id AND own.event='job_failed')
        """,
        (vault_id, since),
    ).fetchone()
    params = {"count": int(count["total"] or 1), "since": since, "last": stamp}
    item = enqueue_notification(
        connection, vault_id=vault_id, event="job_failed_group",
        title="", title_key="notification.job_failed_group.title",
        body_key="notification.job_failed_group.body", message_params=params,
        dedupe_key=group_key, channels=fixed_channels(),
    )
    connection.execute(
        "UPDATE notifications SET problem_status='open', resolved_at=NULL, "
        "message_params=%s, body=%s WHERE id=%s",
        (_params_json(params), translate("notification.job_failed_group.body", **params),
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


def cloud_only_conflict_key(file_id: Any, held_version_id: Any) -> str:
    """The one problem of a Cloud-only Conflict: its file and held version."""
    return f"conflict:{file_id}:{held_version_id}"


def enqueue_cloud_only_conflict(
    connection: Any,
    *,
    vault_id: int,
    file_id: Any,
    held_version_id: Any,
    path: str,
    observed_at: str,
) -> int:
    """Tell the Owner that different content is held back at a cloud-only path.

    It is an open Problem of the Vault, notified once per conflict, until the
    file no longer holds that version (renamed away, removed, or the Owner
    confirmed the replacement). Returns 1 when this call opened it.
    """
    key = cloud_only_conflict_key(file_id, held_version_id)
    existing = connection.execute(
        "SELECT problem_status FROM notifications WHERE dedupe_key=%s", (key,),
    ).fetchone()
    if existing is not None and existing["problem_status"] == "open":
        return 0
    set_problem(
        connection, key=key, event="cloud_only_conflict", vault_id=int(vault_id),
        active=True, params={"path": str(path), "observed_at": str(observed_at)},
    )
    return 1


def _enqueue_incident_notification(
    connection: Any,
    *,
    incident: Mapping[str, Any],
    event: str,
    params: Mapping[str, Any],
) -> int:
    """Notify the owner once per catalog incident."""
    vault_id = int(incident["vault_id"])
    dedupe_key = f"incident:{int(incident['id'])}"
    existing = connection.execute(
        "SELECT id FROM notifications WHERE dedupe_key=%s", (dedupe_key,),
    ).fetchone()
    enqueue_notification(
        connection,
        vault_id=vault_id,
        event=event,
        title="",
        body="",
        title_key=f"notification.{event}.title",
        body_key=f"notification.{event}.body",
        message_params=params,
        dedupe_key=dedupe_key,
        channels=fixed_channels(),
    )
    connection.execute(
        "UPDATE notifications SET problem_status='open' "
        "WHERE dedupe_key=%s AND problem_status IS NULL",
        (dedupe_key,),
    )
    return 0 if existing is not None else 1


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
        from .directory_aggregates import forget_unit_dirty_marks
        forget_unit_dirty_marks(connection)
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
    """Leave the outbox: ending the Job queued it (``job_terminal_notifications``)."""
    result = connection.execute(
        "DELETE FROM job_terminal_notifications WHERE job_id=%s", (job_id,),
    )
    return bool(getattr(result, "rowcount", 0))


def pending_terminal_notification_count(connection: Any) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM job_terminal_notifications"
    ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def reconcile_pending_terminal_notifications(
    connection: Any, *, limit: int = 100
) -> int:
    """Retry terminal notification materialization left pending after a fault."""
    rows = connection.execute(
        """
        SELECT job_id AS id FROM job_terminal_notifications
        ORDER BY queued_at ASC, job_id ASC
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
    _lock_owner_inbox(connection)
    previous = connection.execute(
        "SELECT * FROM notifications WHERE dedupe_key=%s", (key,),
    ).fetchone()
    stamp = _now()
    if not active:
        if previous and previous.get("problem_status") == "open":
            connection.execute(
                "UPDATE notifications SET problem_status='resolved', resolved_at=%s, "
                "read_at=COALESCE(read_at,%s) WHERE id=%s", (stamp, stamp, previous["id"]),
            )
        return None
    context = dict(params or {})
    item = enqueue_notification(
        connection, vault_id=vault_id, event=event,
        title="", title_key=f"notification.{event}.title",
        body_key=f"notification.{event}.body", message_params=context,
        dedupe_key=key, channels=fixed_channels() if email else ("in_app",),
    )
    reopen = previous and previous.get("problem_status") == "resolved"
    connection.execute(
        "UPDATE notifications SET problem_status='open', resolved_at=NULL, "
        "message_params=%s, body=%s, read_at=CASE WHEN %s THEN NULL ELSE read_at END, "
        "created_at=CASE WHEN %s THEN %s ELSE created_at END WHERE id=%s",
        (_params_json(context), translate(f"notification.{event}.body", **context),
         bool(reopen), bool(reopen), stamp, item["id"]),
    )
    if reopen:
        connection.execute(
            "UPDATE notification_deliveries SET status='pending', attempt_count=0, "
            "next_attempt_at=%s, last_error=NULL, updated_at=%s WHERE notification_id=%s",
            (stamp, stamp, item["id"]),
        )
    return _row_notification(connection.execute("SELECT * FROM notifications WHERE id=%s", (item["id"],)).fetchone())


# Each family of problems closes from the state of its own domain. A family is
# reconciled only while one of its problems is open, so reading the inbox costs
# one indexed probe when nothing is open.
_JOB_FAILED_RESOLUTION = """
    n.event='job_failed' AND EXISTS (
        SELECT 1 FROM jobs original WHERE original.id=n.job_id AND {resolution}
    )
"""
_JOB_FAILED_GROUP_RESOLUTION = """
    n.event='job_failed_group' AND NOT EXISTS (
        SELECT 1 FROM jobs original
        WHERE original.vault_id=n.vault_id AND original.action='upload'
          AND original.status='failed'
          AND original.updated_at >= (n.message_params->>'since')::timestamptz
          AND original.updated_at <= COALESCE((n.message_params->>'last')::timestamptz, 'infinity')
          AND NOT EXISTS (SELECT 1 FROM notifications own
                          WHERE own.job_id=original.id AND own.event='job_failed')
          AND NOT {resolution}
    )
"""
# A missing-version problem closes once a listing locates that exact version
# again (or the version is gone through an authorized cleanup); a Cloud-only
# Conflict closes once its file no longer holds that version (#593).
_INCIDENT_RESOLUTION = """
    (n.event='archive_version_missing' AND starts_with(n.dedupe_key, 'missing:')
     AND NOT EXISTS (
        SELECT 1 FROM archive_versions av
        WHERE av.id::text=substr(n.dedupe_key, 9) AND av.availability='missing'
    ))
    OR (n.event='cloud_only_conflict' AND starts_with(n.dedupe_key, 'conflict:')
     AND NOT EXISTS (
        SELECT 1 FROM files f
        WHERE 'conflict:' || f.id::text || ':' || f.held_version_id::text = n.dedupe_key
          AND f.local_presence='present'
    ))
"""
_FILE_UNREADABLE_RESOLUTION = """
    n.event='file_unreadable' AND (
        EXISTS (SELECT 1 FROM vault_component_health h WHERE h.vault_id=n.vault_id
                AND h.component='source' AND h.last_error IS NULL AND h.last_success_at>n.created_at)
        OR EXISTS (SELECT 1 FROM files f
                   WHERE f.vault_id=n.vault_id AND f.path=(n.message_params->>'path')
                     AND f.local_observed_at>n.created_at)
    )
"""

# A failed audit closes with the next successful audit of its Vault (#620), or
# when the Vault is no longer active and so no longer audited.
_CATALOG_AUDIT_FAILED_RESOLUTION = """
    n.event='catalog_audit_failed' AND NOT EXISTS (
        SELECT 1 FROM vault_component_health h JOIN vaults v ON v.id=h.vault_id
        WHERE h.vault_id=n.vault_id AND h.component='audit'
          AND h.last_error IS NOT NULL AND v.lifecycle='active'
    )
"""

# A reconciliation Problem names one blocked file (#628): it closes when no
# file under its key is blocked any more, whichever successful Job cleared the
# block (publication, an unchanged match, a proof of absence) and even when the
# Job that cleared it stopped before it could close the Problem itself.
_UPLOAD_RECONCILIATION_RESOLUTION = """
    n.event='upload_reconciliation' AND starts_with(n.dedupe_key, 'upload_reconciliation:')
    AND NOT EXISTS (
        SELECT 1 FROM files f
        WHERE f.reconciliation_block IS NOT NULL
          AND 'upload_reconciliation:' || f.id::text = n.dedupe_key
    )
"""


def _problem_resolutions() -> dict[frozenset[str], str]:
    from .upload_failure_problems import CLOSED_UPLOAD_FAILURE_SQL as resolution
    return {
        frozenset({"job_failed"}): _JOB_FAILED_RESOLUTION.format(resolution=resolution),
        frozenset({"job_failed_group"}): _JOB_FAILED_GROUP_RESOLUTION.format(resolution=resolution),
        frozenset({"archive_version_missing", "cloud_only_conflict"}): _INCIDENT_RESOLUTION,
        frozenset({"file_unreadable"}): _FILE_UNREADABLE_RESOLUTION,
        frozenset({"catalog_audit_failed"}): _CATALOG_AUDIT_FAILED_RESOLUTION,
        frozenset({"upload_reconciliation"}): _UPLOAD_RECONCILIATION_RESOLUTION,
    }


def reconcile_problem_states(connection: Any) -> None:
    """Refresh catalog/Job-backed problems before counting, listing or emailing.

    No filesystem/S3 operations and no modification of protection evidence.
    Materialized writes require exact completed remediation/cleanup. Pre-write
    failures can also close when a later completed upload protects this same
    file's current content; queued/failed Jobs or stale verified content cannot
    establish that. Reading remains independent of resolution.
    """
    open_events = {
        row["event"] for row in connection.execute(
            "SELECT DISTINCT event FROM notifications WHERE problem_status='open'"
        ).fetchall()
    }
    if not open_events:
        return
    predicates = [
        predicate for events, predicate in _problem_resolutions().items()
        if events & open_events
    ]
    if not predicates:
        return
    stamp = _now()
    connection.execute(
        f"""
        UPDATE notifications n SET problem_status='resolved', resolved_at=%s,
            read_at=COALESCE(read_at,%s)
        WHERE n.problem_status='open' AND (({") OR (".join(predicates)}))
        """,
        (stamp, stamp),
    )


def count_open_problems(connection: Any) -> int:
    reconcile_problem_states(connection)
    row = connection.execute(
        "SELECT count(*) AS total FROM notifications "
        "WHERE in_app_enabled AND problem_status='open'",
    ).fetchone()
    return int(row["total"] or 0)


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
    clauses = ["n.in_app_enabled"]
    params: list[Any] = []

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


def count_unread_notifications(connection: Any) -> int:
    reconcile_problem_states(connection)
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM notifications WHERE in_app_enabled AND read_at IS NULL"
    ).fetchone()
    return int(row["total"] or 0) if row else 0


def mark_notification_read(
    connection: Any,
    *,
    notification_id: int,
    locale: str | None = None,
) -> dict[str, Any] | None:
    reconcile_problem_states(connection)
    row = connection.execute(
        """
        UPDATE notifications
        SET read_at=COALESCE(read_at, %s)
        WHERE id=%s AND in_app_enabled
        RETURNING *
        """,
        (_now(), notification_id),
    ).fetchone()
    return _row_notification(row, locale=locale) if row else None


def mark_all_notifications_read(connection: Any) -> dict[str, int]:
    """Mark every visible unread notification read (issue #225).

    Idempotent: already-read rows and email-only notices are left untouched.
    Returns the number of rows newly stamped plus the authoritative remaining
    unread count.
    """
    reconcile_problem_states(connection)
    result = connection.execute(
        """
        UPDATE notifications
        SET read_at=%s
        WHERE in_app_enabled AND read_at IS NULL
        """,
        (_now(),),
    )
    marked_count = int(getattr(result, "rowcount", 0) or 0)
    return {
        "marked_count": marked_count,
        "unread_count": count_unread_notifications(connection),
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
    stamp = _now()
    pending = connection.execute(
        """
        SELECT d.*, n.event AS n_event, n.title AS n_title, n.body AS n_body,
               n.vault_id AS n_vault_id, n.job_id AS n_job_id,
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


def _delivery_retry_at(attempt: int, *, now: datetime | None = None) -> datetime:
    """Exponential backoff that stays due after ``now`` for the next worker poll."""
    delay = min(DELIVERY_RETRY_CAP_SECONDS, 2 ** max(1, int(attempt)))
    current = now or _now()
    return (current + timedelta(seconds=delay)).replace(microsecond=0)


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
