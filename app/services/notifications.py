"""In-app notifications and outbound delivery (issue #16).

Users choose per-Vault in-app/push events; global administrators manage
shared SMTP/webhook endpoints. Delivery retries are bounded and never mutate
the Job that triggered the notification.

Web Push (issue #72) reuses the same delivery table with channel ``push`` and
subscription rows bound to a Session/device.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from ..config import push_configured, settings
from ..i18n import catalog as locale_catalog
from ..i18n import format_message_params, parse_message_params, translate


SUPPORTED_NOTIFICATION_EVENTS = frozenset({"job_completed", "job_failed"})
SUPPORTED_PERSONAL_NOTIFICATION_CHANNELS = frozenset({"in_app", "push"})


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class WebhookClient(Protocol):
    def send(self, payload: dict[str, Any]) -> None: ...


class SmtpClient(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...


class PushClient(Protocol):
    def send(
        self,
        *,
        subscription: dict[str, Any],
        payload: dict[str, Any],
    ) -> None: ...


def _message_params(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("message_params")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) or raw is None:
        return parse_message_params(raw)
    return {}


def _localized_notification_text(
    row: Mapping[str, Any], *, key_field: str, text_field: str, locale: str | None
) -> str:
    """Render a keyed notification, retaining prose for historical rows.

    A key can be removed or unavailable in a partially upgraded catalog.  In
    that case the durable title/body is safer than returning the key itself.
    """
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
    return translate(key, locale=locale, **_message_params(row))


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
    }


def set_global_webhook_endpoint(
    connection: Any,
    *,
    url: str,
    enabled: bool = True,
    name: str = "default",
) -> dict[str, Any]:
    """Create or update the global webhook endpoint (admin-managed)."""
    stamp = now_iso()
    config_json = json.dumps({"url": url}, sort_keys=True)
    existing = connection.execute(
        """
        SELECT id FROM notification_endpoints
        WHERE kind='webhook' AND name=%s
        """,
        (name,),
    ).fetchone()
    if existing:
        connection.execute(
            """
            UPDATE notification_endpoints
            SET config_json=%s, enabled=%s, updated_at=%s
            WHERE id=%s
            """,
            (config_json, enabled, stamp, existing["id"]),
        )
        endpoint_id = existing["id"]
    else:
        endpoint_id = connection.execute(
            """
            INSERT INTO notification_endpoints(
                kind, name, config_json, enabled, created_at, updated_at
            ) VALUES ('webhook', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, config_json, enabled, stamp, stamp),
        ).fetchone()["id"]
    return {
        "id": endpoint_id,
        "kind": "webhook",
        "name": name,
        "url": url,
        "enabled": enabled,
    }


def set_global_smtp_endpoint(
    connection: Any,
    *,
    host: str,
    port: int = 587,
    username: str = "",
    password: str = "",
    from_address: str = "",
    use_tls: bool = True,
    enabled: bool = True,
    name: str = "default",
) -> dict[str, Any]:
    """Create or update the global SMTP endpoint (admin-managed)."""
    stamp = now_iso()
    config = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_address": from_address,
        "use_tls": use_tls,
    }
    config_json = json.dumps(config, sort_keys=True)
    existing = connection.execute(
        """
        SELECT id FROM notification_endpoints
        WHERE kind='smtp' AND name=%s
        """,
        (name,),
    ).fetchone()
    if existing:
        connection.execute(
            """
            UPDATE notification_endpoints
            SET config_json=%s, enabled=%s, updated_at=%s
            WHERE id=%s
            """,
            (config_json, enabled, stamp, existing["id"]),
        )
        endpoint_id = existing["id"]
    else:
        endpoint_id = connection.execute(
            """
            INSERT INTO notification_endpoints(
                kind, name, config_json, enabled, created_at, updated_at
            ) VALUES ('smtp', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, config_json, enabled, stamp, stamp),
        ).fetchone()["id"]
    return {"id": endpoint_id, "kind": "smtp", "name": name, "enabled": enabled}


def _normalize_recipient_user_ids(values: Any) -> list[int]:
    """Read legacy recipient lists without trusting malformed JSON or types."""
    if isinstance(values, str):
        try:
            values = json.loads(values or "[]")
        except json.JSONDecodeError:
            values = []
    if not isinstance(values, (list, tuple, set)):
        return []
    recipients: list[int] = []
    for value in values:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate > 0 and candidate not in recipients:
            recipients.append(candidate)
    return recipients


def set_vault_notification_preference(
    connection: Any,
    *,
    vault_id: int,
    event: str,
    channel: str,
    enabled: bool = True,
    recipient_user_ids: list[int] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Keep the legacy vault-wide endpoint preference API intact.

    Issue #75 stores personal inbox choices in a separate table.  This helper
    remains for the existing webhook/email administration seam; notably, a
    malformed or missing legacy recipient list is treated as empty rather than
    leaking into the personal preference table.
    """
    if user_id is not None:
        return set_user_vault_notification_preference(
            connection,
            user_id=user_id,
            vault_id=vault_id,
            event=event,
            channel=channel,
            enabled=enabled,
        )
    if channel not in {"in_app", "webhook", "email"}:
        raise ValueError(f"invalid notification channel: {channel}")
    recipients = _normalize_recipient_user_ids(recipient_user_ids)
    recipients_json = json.dumps(recipients)
    row = connection.execute(
        """
        INSERT INTO vault_notification_preferences(
            vault_id, event, channel, enabled, recipient_user_ids_json
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(vault_id, event, channel) DO UPDATE SET
            enabled=excluded.enabled,
            recipient_user_ids_json=excluded.recipient_user_ids_json
        RETURNING *
        """,
        (vault_id, event, channel, enabled, recipients_json),
    ).fetchone()
    return {
        "id": row["id"],
        "vault_id": row["vault_id"],
        "event": row["event"],
        "channel": row["channel"],
        "enabled": bool(row["enabled"]),
        "recipient_user_ids": _normalize_recipient_user_ids(
            row.get("recipient_user_ids_json")
        ),
    }


def _active_vault_member(
    connection: Any, *, user_id: int, vault_id: int
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM vault_members vm
        JOIN users u ON u.id=vm.user_id
        JOIN vaults v ON v.id=vm.vault_id
        WHERE vm.user_id=%s AND vm.vault_id=%s
          AND u.active=TRUE AND v.enabled=TRUE
          AND v.decommission_state='active'
        """,
        (user_id, vault_id),
    ).fetchone()
    return row is not None


def set_user_vault_notification_preference(
    connection: Any,
    *,
    user_id: int,
    vault_id: int,
    event: str,
    channel: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Set one personal preference, never a preference for another User."""
    if event not in SUPPORTED_NOTIFICATION_EVENTS:
        raise ValueError(f"invalid personal notification event: {event}")
    if channel not in SUPPORTED_PERSONAL_NOTIFICATION_CHANNELS:
        raise ValueError(f"invalid personal notification channel: {channel}")
    if not _active_vault_member(connection, user_id=user_id, vault_id=vault_id):
        raise ValueError("The acting user is not an active member of this vault")
    row = connection.execute(
        """
        INSERT INTO user_vault_notification_preferences(
            user_id, vault_id, event, channel, enabled
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(user_id, vault_id, event, channel) DO UPDATE SET
            enabled=excluded.enabled
        RETURNING *
        """,
        (user_id, vault_id, event, channel, enabled),
    ).fetchone()
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "vault_id": row["vault_id"],
        "event": row["event"],
        "channel": row["channel"],
        "enabled": bool(row["enabled"]),
    }


def list_user_vault_notification_preferences(
    connection: Any, *, user_id: int, vault_id: int
) -> list[dict[str, Any]]:
    """List only the acting User's preferences for one active Vault."""
    if not _active_vault_member(connection, user_id=user_id, vault_id=vault_id):
        return []
    rows = connection.execute(
        """
        SELECT id, user_id, vault_id, event, channel, enabled
        FROM user_vault_notification_preferences
        WHERE user_id=%s AND vault_id=%s
        ORDER BY event, channel
        """,
        (user_id, vault_id),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "vault_id": row["vault_id"],
            "event": row["event"],
            "channel": row["channel"],
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    ]


def _user_vault_preference_map(
    connection: Any, *, user_id: int, vault_id: int, event: str
) -> dict[str, bool]:
    rows = connection.execute(
        """
        SELECT channel, enabled
        FROM user_vault_notification_preferences
        WHERE user_id=%s AND vault_id=%s AND event=%s
        """,
        (user_id, vault_id, event),
    ).fetchall()
    return {str(row["channel"]): bool(row["enabled"]) for row in rows}


def _legacy_in_app_preference(
    connection: Any, *, user_id: int, vault_id: int, event: str
) -> bool | None:
    """Translate one legacy vault preference without widening its recipients."""
    row = connection.execute(
        """
        SELECT enabled, recipient_user_ids_json
        FROM vault_notification_preferences
        WHERE vault_id=%s AND event=%s AND channel='in_app'
        """,
        (vault_id, event),
    ).fetchone()
    if row is None:
        return None
    recipients = _normalize_recipient_user_ids(row.get("recipient_user_ids_json"))
    if recipients and user_id not in recipients:
        return False
    return bool(row["enabled"])


def _ensure_delivery(
    connection: Any, *, notification_id: int, channel: str, stamp: str
) -> None:
    if channel == "in_app":
        return
    if channel not in {"webhook", "email", "push"}:
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

    ``in_app_enabled`` is persisted on the canonical row, so a push-only row
    can share the same delivery model without becoming visible in the inbox.
    ``dedupe_key`` is optional for legacy callers and unique per User when set.
    """
    requested_channels = tuple(dict.fromkeys(channels))
    for channel in requested_channels:
        if channel not in {"in_app", "webhook", "email", "push"}:
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


def upsert_push_subscription(
    connection: Any,
    *,
    user_id: int,
    session_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
) -> dict[str, Any]:
    """Persist a Web Push subscription against the current User and Session."""
    stamp = now_iso()
    existing = connection.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint=%s",
        (endpoint,),
    ).fetchone()
    if existing:
        connection.execute(
            """
            UPDATE push_subscriptions
            SET user_id=%s, session_id=%s, p256dh=%s, auth=%s, updated_at=%s
            WHERE id=%s
            """,
            (user_id, session_id, p256dh, auth, stamp, existing["id"]),
        )
        subscription_id = existing["id"]
    else:
        subscription_id = connection.execute(
            """
            INSERT INTO push_subscriptions(
                user_id, session_id, endpoint, p256dh, auth, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, session_id, endpoint, p256dh, auth, stamp, stamp),
        ).fetchone()["id"]
    return {
        "id": subscription_id,
        "user_id": user_id,
        "session_id": session_id,
        "endpoint": endpoint,
    }


def delete_push_subscriptions_for_session(connection: Any, session_id: str) -> int:
    result = connection.execute(
        "DELETE FROM push_subscriptions WHERE session_id=%s",
        (session_id,),
    )
    return int(getattr(result, "rowcount", 0) or 0)


def list_deliverable_push_subscriptions(
    connection: Any,
    *,
    user_id: int,
    vault_id: int | None,
) -> list[dict[str, Any]]:
    """Subscriptions whose Session is live and User can still see the Vault."""
    stamp = now_iso()
    if vault_id is None:
        rows = connection.execute(
            """
            SELECT ps.endpoint, ps.p256dh, ps.auth
            FROM push_subscriptions ps
            JOIN sessions s ON s.id = ps.session_id
            WHERE ps.user_id=%s
              AND s.revoked_at IS NULL
              AND s.absolute_expires_at > %s
              AND s.idle_expires_at > %s
            """,
            (user_id, stamp, stamp),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT ps.endpoint, ps.p256dh, ps.auth
            FROM push_subscriptions ps
            JOIN sessions s ON s.id = ps.session_id
            JOIN vault_members vm
              ON vm.user_id = ps.user_id AND vm.vault_id = %s
            WHERE ps.user_id=%s
              AND s.revoked_at IS NULL
              AND s.absolute_expires_at > %s
              AND s.idle_expires_at > %s
            """,
            (vault_id, user_id, stamp, stamp),
        ).fetchall()
    return [
        {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        for row in rows
    ]


def enqueue_job_terminal_push(connection: Any, *, job_id: int) -> int:
    """Persist one canonical completion/failure notification per member.

    The old function name is retained for the worker call sites.  A canonical
    row is written whenever in-app is enabled, even with no VAPID
    configuration.  Push remains asynchronous and is attached to that same
    row only when VAPID, the preference, and a live subscription all permit it.
    With no personal preference yet, both in-app and configured push keep their
    enabled defaults for the terminal events.
    """
    job = connection.execute(
        """
        SELECT id, vault_id, path, action, status
        FROM jobs WHERE id=%s
        """,
        (job_id,),
    ).fetchone()
    if not job or job["status"] not in {"completed", "failed"}:
        return 0
    vault_id = int(job["vault_id"])
    event = "job_completed" if job["status"] == "completed" else "job_failed"
    title_key = f"notification.{event}.title"
    body_key = f"notification.{event}.body"
    params = {"action": str(job["action"]), "path": str(job["path"])}
    members = connection.execute(
        """
        SELECT vm.user_id
        FROM vault_members vm
        JOIN users u ON u.id=vm.user_id
        WHERE vm.vault_id=%s AND u.active=TRUE
        ORDER BY vm.user_id
        """,
        (vault_id,),
    ).fetchall()

    enqueued = 0
    for member in members:
        user_id = int(member["user_id"])
        preferences = _user_vault_preference_map(
            connection, user_id=user_id, vault_id=vault_id, event=event
        )
        # In-app defaults to enabled so the inbox works before a user has
        # visited the preferences UI. A matching legacy Vault preference
        # remains usable, but its recipient list can only narrow delivery to
        # the current active member.
        if "in_app" in preferences:
            in_app_enabled = preferences["in_app"]
        else:
            legacy_in_app = _legacy_in_app_preference(
                connection, user_id=user_id, vault_id=vault_id, event=event
            )
            in_app_enabled = (
                legacy_in_app if legacy_in_app is not None else True
            )
        # Push defaults to the previous issue #72 behaviour until a personal
        # push preference is saved, but an explicit false always wins.
        push_enabled = preferences.get("push", True)
        subscriptions: list[dict[str, Any]] = []
        if push_enabled and push_configured():
            subscriptions = list_deliverable_push_subscriptions(
                connection, user_id=user_id, vault_id=vault_id
            )
        channels: list[str] = []
        if in_app_enabled:
            channels.append("in_app")
        if subscriptions:
            channels.append("push")
        if not channels:
            continue
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
            in_app_enabled=in_app_enabled,
            dedupe_key=dedupe_key,
            channels=tuple(channels),
        )
        if existing is None:
            enqueued += 1
    return enqueued


def enqueue_job_terminal_notification(connection: Any, *, job_id: int) -> int:
    """Descriptive alias for the canonical terminal notification enqueue."""
    return enqueue_job_terminal_push(connection, job_id=job_id)


def enqueue_job_terminal_notification_best_effort(
    connection: Any, *, job_id: int
) -> int:
    """Enqueue a terminal notification without weakening the Job transition.

    The caller's transaction owns the authoritative Job update.  A failed
    notification insert can put a PostgreSQL transaction into the failed state
    (and can leave partial rows on either backend), so isolate the best-effort
    work in a real savepoint rather than merely catching the exception.
    """
    savepoint = "job_terminal_notification"
    try:
        connection.execute(f"SAVEPOINT {savepoint}")
    except Exception:
        # A normal SQLite/PostgreSQL transaction always supports SAVEPOINT.  If
        # a connection wrapper does not, skip optional notification work rather
        # than making the already-authoritative Job transition fail.
        return 0

    try:
        enqueued = enqueue_job_terminal_push(connection, job_id=job_id)
    except Exception:
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        finally:
            # RELEASE is required after ROLLBACK TO so the outer transaction
            # remains usable on both SQLite and PostgreSQL.
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return 0

    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    return enqueued


def _visible_notification_predicate(alias: str = "n") -> str:
    """Return the inbox visibility predicate for a SELECT or UPDATE.

    SELECT callers pass the explicit ``n`` alias.  UPDATE deliberately passes
    an empty alias: SQLite and PostgreSQL both support the target table's
    unaliased name in the correlated membership check, while UPDATE alias
    syntax is not shared consistently by all supported forms.
    """
    prefix = f"{alias}." if alias else ""
    outer = prefix or "notifications."
    return f"""
        {prefix}user_id=%s
        AND COALESCE({prefix}in_app_enabled, TRUE)=TRUE
        AND (
            {prefix}vault_id IS NULL
            OR EXISTS (
                SELECT 1 FROM vault_members vm
                WHERE vm.vault_id={outer}vault_id
                  AND vm.user_id={outer}user_id
            )
        )
    """


def list_in_app_notifications(
    connection: Any,
    *,
    user_id: int,
    limit: int = 50,
    locale: str | None = None,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT n.* FROM notifications n
        WHERE {_visible_notification_predicate()}
        ORDER BY n.id DESC
        LIMIT %s
        """,
        (user_id, max(1, min(int(limit), 200))),
    ).fetchall()
    return [_row_notification(row, locale=locale) for row in rows]


def count_unread_notifications(connection: Any, *, user_id: int) -> int:
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


def _endpoint_config(connection: Any, kind: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT config_json, enabled FROM notification_endpoints
        WHERE kind=%s AND name='default'
        """,
        (kind,),
    ).fetchone()
    if not row or not row["enabled"]:
        return None
    try:
        return json.loads(row["config_json"] or "{}")
    except json.JSONDecodeError:
        return None


def deliver_pending_notifications(
    connection: Any,
    *,
    webhook_client: WebhookClient | None = None,
    smtp_client: SmtpClient | None = None,
    push_client: PushClient | None = None,
    max_attempts: int = 3,
) -> dict[str, int]:
    """Attempt pending webhook/email/push deliveries without touching Job rows."""
    stamp = now_iso()
    pending = connection.execute(
        """
        SELECT d.*, n.event AS n_event, n.title AS n_title, n.body AS n_body,
               n.user_id AS n_user_id, n.vault_id AS n_vault_id, n.job_id AS n_job_id
        FROM notification_deliveries d
        JOIN notifications n ON n.id = d.notification_id
        WHERE d.status = 'pending'
          AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= %s)
        ORDER BY d.id
        """,
        (stamp,),
    ).fetchall()

    stats = {"attempted": 0, "delivered": 0, "failed": 0, "requeued": 0}
    webhook_config = _endpoint_config(connection, "webhook")
    smtp_config = _endpoint_config(connection, "smtp")

    for row in pending:
        stats["attempted"] += 1
        attempt = int(row["attempt_count"]) + 1
        error: str | None = None
        try:
            if row["channel"] == "webhook":
                if webhook_client is None or webhook_config is None:
                    raise RuntimeError("webhook endpoint unavailable")
                webhook_client.send(
                    {
                        "event": row["n_event"],
                        "title": row["n_title"],
                        "body": row["n_body"],
                        "user_id": row["n_user_id"],
                        "vault_id": row["n_vault_id"],
                        "job_id": row["n_job_id"],
                        "url": webhook_config.get("url"),
                    }
                )
            elif row["channel"] == "email":
                if smtp_client is None or smtp_config is None:
                    raise RuntimeError("smtp endpoint unavailable")
                from .email_templates import render_email

                rendered = render_email(
                    row["n_event"],
                    title=row["n_title"],
                    body=row["n_body"],
                    vault_id=row["n_vault_id"],
                )
                smtp_client.send(
                    {
                        "template": row["n_event"],
                        "subject": rendered["subject"],
                        "body": rendered["body"],
                        "user_id": row["n_user_id"],
                        "from_address": smtp_config.get("from_address"),
                        "host": smtp_config.get("host"),
                    }
                )
            elif row["channel"] == "push":
                if not push_configured() or push_client is None:
                    raise RuntimeError("push endpoint unavailable")
                if row["n_vault_id"] is not None:
                    preferences = _user_vault_preference_map(
                        connection,
                        user_id=int(row["n_user_id"]),
                        vault_id=int(row["n_vault_id"]),
                        event=str(row["n_event"]),
                    )
                    if not preferences.get("push", True):
                        # A later opt-out applies before the asynchronous
                        # delivery pass and does not expose a stale push.
                        subscriptions = []
                    else:
                        subscriptions = list_deliverable_push_subscriptions(
                            connection,
                            user_id=int(row["n_user_id"]),
                            vault_id=row["n_vault_id"],
                        )
                else:
                    subscriptions = list_deliverable_push_subscriptions(
                        connection,
                        user_id=int(row["n_user_id"]),
                        vault_id=row["n_vault_id"],
                    )
                if not subscriptions:
                    # Security: revoked Session / removed membership → no delivery.
                    pass
                else:
                    payload = {
                        "title": row["n_title"],
                        "body": row["n_body"],
                        "data": {
                            "event": row["n_event"],
                            "job_id": row["n_job_id"],
                            "vault_id": row["n_vault_id"],
                            "url": "/",
                        },
                    }
                    for subscription in subscriptions:
                        push_client.send(
                            subscription=subscription,
                            payload=payload,
                        )
            else:
                raise RuntimeError(f"unsupported channel: {row['channel']}")
        except Exception as exc:  # system boundary failure
            error = str(exc)

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
            connection.execute(
                """
                UPDATE notification_deliveries
                SET status='pending', attempt_count=%s, last_error=%s,
                    next_attempt_at=%s, updated_at=%s
                WHERE id=%s
                """,
                (attempt, error[:500], stamp, stamp, row["id"]),
            )
            stats["requeued"] += 1
    return stats


class PyWebPushClient:
    """Production PushClient backed by pywebpush when the package is installed."""

    def send(
        self,
        *,
        subscription: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        from pywebpush import webpush

        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key.strip(),
            vapid_claims={"sub": settings.vapid_subject.strip() or "mailto:admin@localhost"},
        )
