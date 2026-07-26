"""In-app notifications and outbound delivery (issue #16).

Owners choose per-vault events and channels; global administrators manage
shared SMTP/webhook endpoints. Delivery retries are bounded and never mutate
the Job that triggered the notification.

Web Push (issue #72) reuses the same delivery table with channel ``push`` and
subscription rows bound to a Session/device.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from ..config import push_configured, settings


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


def _row_notification(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "vault_id": row.get("vault_id"),
        "job_id": row.get("job_id"),
        "event": row["event"],
        "title": row["title"],
        "body": row.get("body") or "",
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


def set_vault_notification_preference(
    connection: Any,
    *,
    vault_id: int,
    event: str,
    channel: str,
    enabled: bool = True,
    recipient_user_ids: list[int] | None = None,
) -> dict[str, Any]:
    if channel not in {"in_app", "webhook", "email"}:
        raise ValueError(f"invalid notification channel: {channel}")
    recipients = list(recipient_user_ids or [])
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
        "recipient_user_ids": recipients,
    }


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
) -> dict[str, Any]:
    """Create an in-app notification and optional outbound delivery rows."""
    stamp = now_iso()
    row = connection.execute(
        """
        INSERT INTO notifications(
            user_id, vault_id, job_id, event, title, body, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (user_id, vault_id, job_id, event, title, body, stamp),
    ).fetchone()
    notification = _row_notification(row)
    for channel in channels:
        if channel == "in_app":
            continue
        if channel not in {"webhook", "email", "push"}:
            raise ValueError(f"invalid delivery channel: {channel}")
        connection.execute(
            """
            INSERT INTO notification_deliveries(
                notification_id, channel, status, attempt_count,
                next_attempt_at, updated_at
            ) VALUES (%s, %s, 'pending', 0, %s, %s)
            """,
            (notification["id"], channel, stamp, stamp),
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
    """Enqueue push deliveries for vault members when a Job completes or fails.

    No-ops when VAPID is unconfigured (seam 7).
    """
    if not push_configured():
        return 0
    job = connection.execute(
        "SELECT id, vault_id, path, action, status FROM jobs WHERE id=%s",
        (job_id,),
    ).fetchone()
    if not job or job["status"] not in {"completed", "failed"}:
        return 0
    members = connection.execute(
        "SELECT user_id FROM vault_members WHERE vault_id=%s",
        (job["vault_id"],),
    ).fetchall()
    event = "job_completed" if job["status"] == "completed" else "job_failed"
    title = (
        "Job completed"
        if job["status"] == "completed"
        else "Job failed"
    )
    body = f"{job['action']}: {job['path']}"
    enqueued = 0
    for member in members:
        user_id = int(member["user_id"])
        subs = list_deliverable_push_subscriptions(
            connection, user_id=user_id, vault_id=int(job["vault_id"])
        )
        if not subs:
            continue
        enqueue_notification(
            connection,
            user_id=user_id,
            vault_id=int(job["vault_id"]),
            job_id=int(job["id"]),
            event=event,
            title=title,
            body=body,
            channels=("push",),
        )
        enqueued += 1
    return enqueued


def list_in_app_notifications(
    connection: Any,
    *,
    user_id: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM notifications
        WHERE user_id=%s
        ORDER BY id DESC
        LIMIT %s
        """,
        (user_id, max(1, min(limit, 200))),
    ).fetchall()
    return [_row_notification(row) for row in rows]


def mark_notification_read(
    connection: Any, *, notification_id: int, user_id: int
) -> dict[str, Any] | None:
    stamp = now_iso()
    connection.execute(
        """
        UPDATE notifications
        SET read_at=COALESCE(read_at, %s)
        WHERE id=%s AND user_id=%s
        """,
        (stamp, notification_id, user_id),
    )
    row = connection.execute(
        "SELECT * FROM notifications WHERE id=%s AND user_id=%s",
        (notification_id, user_id),
    ).fetchone()
    return _row_notification(row) if row else None


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
