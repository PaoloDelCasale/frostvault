"""Shared SQLite/PostgreSQL assertions for the #414 schema cut."""
from __future__ import annotations

from typing import Any

from app.catalog import ArchiveCatalog
from app.sessions import create_session

REMOVED_TABLES = {
    "notification_endpoints",
    "vault_notification_preferences",
    "user_vault_notification_preferences",
    "push_subscriptions",
}
PRESERVED_TABLES = (
    "users", "sessions", "vaults", "vault_files", "file_paths", "local_copies",
    "archive_versions", "jobs", "notifications",
)
STAMP = "2026-11-03T00:00:00+00:00"


def seed_legacy_notifications(connection: Any) -> None:
    connection.execute(
        "INSERT INTO users(id, username, display_name, password_hash) "
        "VALUES (1, 'owner', 'Owner', 'hash')"
    )
    connection.execute(
        """
        INSERT INTO vaults(
            id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
        ) VALUES (2, 'photos', 'Photos', '/source', 'bucket', 'photos', 'remote')
        """
    )
    catalog = ArchiveCatalog(connection)
    file_id = catalog.observe_local_copy(
        vault_id=2, path="photo.jpg", file_type="regular", size=10,
        mtime_ns=1, observed_at=STAMP,
    )
    version_id = catalog.record_archive_version(
        vault_id=2, path="photo.jpg", object_key="photos/photo.jpg",
        provider_version_id="only-cloud-version", size=10, storage_class="STANDARD",
        etag="etag", uploaded_at=STAMP, observed_at=STAMP, scan_id=STAMP,
    )
    catalog.mark_version_verified(version_id, plaintext_sha256="a" * 64, verified_at=STAMP)
    catalog.mark_local_copy_missing(file_id, observed_at=STAMP)
    connection.execute(
        """
        INSERT INTO jobs(
            id, vault_id, vault_file_id, path, action, status, requested_at, updated_at
        ) VALUES (3, 2, %s, 'photo.jpg', 'upload', 'completed', %s, %s)
        """,
        (file_id, STAMP, STAMP),
    )
    create_session(connection, user_id=1, auth_method="local")
    session_id = connection.execute("SELECT id FROM sessions").fetchone()["id"]
    for kind in ("smtp", "webhook"):
        connection.execute(
            "INSERT INTO notification_endpoints(kind, config_json, created_at, updated_at) "
            "VALUES (%s, '{}', %s, %s)", (kind, STAMP, STAMP),
        )
    connection.execute(
        "INSERT INTO vault_notification_preferences(vault_id, event, channel, enabled) "
        "VALUES (2, 'job_failed', 'email', FALSE)"
    )
    connection.execute(
        "INSERT INTO user_vault_notification_preferences(user_id, vault_id, event, channel, enabled) "
        "VALUES (1, 2, 'job_failed', 'in_app', FALSE)"
    )
    connection.execute(
        """
        INSERT INTO push_subscriptions(
            user_id, session_id, endpoint, p256dh, auth, created_at, updated_at
        ) VALUES (1, %s, 'https://example.test/device', 'test-key', 'test-auth', %s, %s)
        """,
        (session_id, STAMP, STAMP),
    )
    connection.execute(
        """
        INSERT INTO notifications(
            id, user_id, vault_id, job_id, event, title, body, created_at,
            read_at, title_key, body_key, message_params, dedupe_key, in_app_enabled
        ) VALUES (
            4, 1, 2, 3, 'job_failed', 'Historical title', 'Historical body', %s,
            %s, 'notification.job_failed.title', 'notification.job_failed.body',
            '{"action":"upload","path":"photo.jpg"}', 'job:3:job_failed', TRUE
        )
        """,
        (STAMP, STAMP),
    )
    for channel in ("email", "webhook", "push"):
        connection.execute(
            """
            INSERT INTO notification_deliveries(
                notification_id, channel, status, attempt_count, next_attempt_at,
                last_error, updated_at
            ) VALUES (4, %s, 'pending', 1, %s, 'previous failure', %s)
            """,
            (channel, STAMP, STAMP),
        )


def preserved_state(connection: Any) -> dict[str, list[dict[str, Any]]]:
    state = {
        table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
        for table in PRESERVED_TABLES
    }
    state["notification_deliveries"] = [
        dict(row) for row in connection.execute(
            "SELECT * FROM notification_deliveries WHERE channel='email' ORDER BY id"
        ).fetchall()
    ]
    return state
