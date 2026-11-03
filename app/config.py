from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_FRONTEND_DIST = str(
    Path(__file__).resolve().parent.parent / "frontend" / "dist"
)


def as_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_placeholder(value: str, *current_values: str) -> bool:
    normalized = value.strip().upper()
    return normalized.startswith(
        ("REPLACE", "CHANGE-ME", "EXAMPLE-", "SOSTITUISCI", "INSERISCI")
    ) or normalized in {
        item.upper() for item in current_values
    }


@dataclass(frozen=True)
class Settings:
    db_backend: str = os.getenv("DB_BACKEND", "postgresql").strip().lower()
    sqlite_path: str = os.getenv("SQLITE_PATH", "/data/frostvault.db")
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "frostvault_session")
    csrf_cookie_name: str = os.getenv("CSRF_COOKIE_NAME", "frostvault_csrf")
    session_idle_seconds: int = max(
        60, int(os.getenv("SESSION_IDLE_SECONDS", str(12 * 60 * 60)))
    )
    session_absolute_seconds: int = max(
        300, int(os.getenv("SESSION_ABSOLUTE_SECONDS", str(7 * 24 * 60 * 60)))
    )
    cookie_secure: bool = as_bool("COOKIE_SECURE", False)
    allowed_hosts: str = os.getenv("ALLOWED_HOSTS", "")
    trusted_proxies: str = os.getenv("TRUSTED_PROXIES", "")
    reauth_window_seconds: int = max(
        60, int(os.getenv("REAUTH_WINDOW_SECONDS", "600"))
    )
    rclone_config: str = os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf")
    aws_region: str = os.getenv("AWS_DEFAULT_REGION", "eu-south-1")
    scan_interval: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "21600"))
    audit_interval: int = int(os.getenv("AUDIT_INTERVAL_SECONDS", str(7 * 24 * 60 * 60)))
    filesystem_watch_enabled: bool = as_bool("FILESYSTEM_WATCH_ENABLED", True)
    filesystem_watch_force_polling: bool = as_bool(
        "FILESYSTEM_WATCH_FORCE_POLLING", False
    )
    filesystem_watch_debounce_ms: int = max(
        100, int(os.getenv("FILESYSTEM_WATCH_DEBOUNCE_MS", "1200"))
    )
    filesystem_watch_poll_ms: int = max(
        100, int(os.getenv("FILESYSTEM_WATCH_POLL_MS", "2000"))
    )
    queue_poll_interval: int = int(os.getenv("QUEUE_POLL_SECONDS", "5"))
    operation_concurrency: int = max(
        1,
        min(
            16,
            int(
                os.getenv(
                    "OPERATION_CONCURRENCY",
                    os.getenv("UPLOAD_CONCURRENCY", "4"),
                )
            ),
        ),
    )
    restore_poll_interval: int = int(os.getenv("JOB_POLL_SECONDS", "900"))
    restore_days: int = int(os.getenv("RESTORE_DAYS", "3"))
    restore_tier: str = os.getenv("RESTORE_TIER", "Bulk")
    restore_high_impact_gib: float = float(
        os.getenv("RESTORE_HIGH_IMPACT_GIB", "100")
    )
    restore_high_impact_eur: float = float(
        os.getenv("RESTORE_HIGH_IMPACT_EUR", "10")
    )
    restore_approval_hold_seconds: int = max(
        60, int(os.getenv("RESTORE_APPROVAL_HOLD_SECONDS", "3600"))
    )
    cloud_purge_delay_seconds: int = max(
        60, int(os.getenv("CLOUD_PURGE_DELAY_SECONDS", str(24 * 60 * 60)))
    )
    allow_local_delete: bool = as_bool("ALLOW_LOCAL_DELETE", False)
    # Optional global upload bandwidth cap in KiB/s (#413). None/0 means
    # unlimited; recoveries and other transfers are never throttled.
    bandwidth_limit_kibps: int | None = (
        None
        if not os.getenv("BANDWIDTH_LIMIT_KIBPS", "").strip()
        else max(0, int(os.getenv("BANDWIDTH_LIMIT_KIBPS", "0")))
    )
    # Plain-vault recovery uses boto3 TransferManager parallel range GETs above
    # the multipart threshold. Crypt recoveries use rclone multi-thread streams.
    s3_download_max_concurrency: int = max(
        1, min(32, int(os.getenv("S3_DOWNLOAD_MAX_CONCURRENCY", "10")))
    )
    s3_download_multipart_threshold_mib: int = max(
        5, int(os.getenv("S3_DOWNLOAD_MULTIPART_THRESHOLD_MIB", "8"))
    )
    s3_download_multipart_chunksize_mib: int = max(
        5, int(os.getenv("S3_DOWNLOAD_MULTIPART_CHUNKSIZE_MIB", "8"))
    )
    rclone_multi_thread_streams: int = max(
        1, min(32, int(os.getenv("RCLONE_MULTI_THREAD_STREAMS", "8")))
    )
    rclone_multi_thread_cutoff_mib: int = max(
        1, int(os.getenv("RCLONE_MULTI_THREAD_CUTOFF_MIB", "64"))
    )
    # Cap Job progress DB writes during transfers (ms between updates).
    job_progress_min_interval_ms: int = max(
        50, int(os.getenv("JOB_PROGRESS_MIN_INTERVAL_MS", "500"))
    )

    # LAN networks from which the owner's emergency password is accepted, in
    # addition to loopback (#412). Empty means loopback only.
    break_glass_allowed_cidrs: str = os.getenv("BREAK_GLASS_ALLOWED_CIDRS", "")

    oidc_enabled: bool = as_bool("OIDC_ENABLED", False)
    oidc_issuer: str = os.getenv("OIDC_ISSUER", "")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "")
    oidc_scopes: str = os.getenv("OIDC_SCOPES", "openid email profile")
    oidc_login_ttl_seconds: int = max(
        60, int(os.getenv("OIDC_LOGIN_TTL_SECONDS", "600"))
    )

    # The owner's local credentials: the emergency password accepted only
    # from loopback and BREAK_GLASS_ALLOWED_CIDRS. Applied at every start.
    bootstrap_admin_username: str = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "")
    bootstrap_admin_password: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    bootstrap_admin_display_name: str = os.getenv("BOOTSTRAP_ADMIN_DISPLAY_NAME", "Administrator")
    # Vault creation (issue #7): the server -- never the caller -- derives
    # every vault's storage namespace from a generated UUID.
    # Local empty-Vault roots use the fixed /sources/managed/<uuid> contract
    # (issue #148); vault_s3_bucket/vault_rclone_remote are the shared
    # bucket/remote assigned to plain vaults.
    vault_s3_bucket: str = os.getenv("VAULT_S3_BUCKET", os.getenv("S3_BUCKET", ""))
    vault_rclone_remote: str = os.getenv("VAULT_RCLONE_REMOTE", "")
    # Underlying S3 (or compatible) remote that per-vault crypt remotes wrap
    # at runtime (issue #6). Distinct from VAULT_RCLONE_REMOTE, which may be a
    # shared legacy crypt/alias remote used by plain vaults.
    vault_rclone_base_remote: str = os.getenv("VAULT_RCLONE_BASE_REMOTE", "")
    # Fernet key (url-safe base64-encoded 32 bytes) that seals per-vault crypt
    # secrets at rest. Required to create or operate crypt vaults.
    archive_master_key: str = os.getenv("ARCHIVE_MASTER_KEY", "")

    # When true (default), bring the DB schema to HEAD on application start
    # (fresh → alembic; existing behind HEAD → pre-upgrade backup + alembic).
    # Set AUTO_MIGRATE=0 for CI/manual upgrade workflows.
    auto_migrate: bool = as_bool("AUTO_MIGRATE", True)

    # Encrypted metadata database backups (issue #15). Artifacts land on a
    # dedicated local volume and under the encrypted system/backups/ S3 prefix.
    # The master key seals artifacts but is never written into them.
    metadata_backup_dir: str = os.getenv("METADATA_BACKUP_DIR", "/data/backups")
    metadata_backup_retention: int = max(
        1, int(os.getenv("METADATA_BACKUP_RETENTION", "14"))
    )
    metadata_backup_interval_seconds: int = max(
        0, int(os.getenv("METADATA_BACKUP_INTERVAL_SECONDS", str(24 * 60 * 60)))
    )
    metadata_backup_s3_prefix: str = os.getenv(
        "METADATA_BACKUP_S3_PREFIX", "system/backups/"
    )
    metadata_backup_verify_interval_seconds: int = max(
        0,
        int(os.getenv("METADATA_BACKUP_VERIFY_INTERVAL_SECONDS", str(7 * 24 * 60 * 60))),
    )

    # Built React SPA (Vite) directory served for all HTML routes.
    frontend_dist_dir: str = os.getenv("FRONTEND_DIST_DIR", _DEFAULT_FRONTEND_DIST)

    # Optional installation-wide email delivery, owned by the deployment (#414).
    # All three addresses/host inputs are required; without them only the inbox
    # receives events. Credentials are never stored in the database.
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_address: str = os.getenv("SMTP_FROM_ADDRESS", "")
    smtp_to_address: str = os.getenv("SMTP_TO_ADDRESS", "")
    smtp_use_tls: bool = as_bool("SMTP_USE_TLS", True)


settings = Settings()


def validate_settings() -> None:
    if not 1 <= settings.smtp_port <= 65535:
        raise RuntimeError("SMTP_PORT must be between 1 and 65535")
    if settings.db_backend not in {"sqlite", "postgresql"}:
        raise RuntimeError("DB_BACKEND must be sqlite or postgresql")
    for entry in settings.trusted_proxies.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            raise RuntimeError(
                "TRUSTED_PROXIES contains an invalid network: " f"{candidate}"
            )
    if settings.cookie_secure:
        # A secure cookie means the app sits behind a reverse proxy on the public
        # internet, so host and forwarded hardening must be configured explicitly.
        if not settings.allowed_hosts.strip():
            raise RuntimeError(
                "ALLOWED_HOSTS is required when COOKIE_SECURE is enabled"
            )
        if not settings.trusted_proxies.strip():
            raise RuntimeError(
                "TRUSTED_PROXIES is required when COOKIE_SECURE is enabled"
            )
    if settings.oidc_enabled:
        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER", settings.oidc_issuer),
                ("OIDC_CLIENT_ID", settings.oidc_client_id),
                ("OIDC_CLIENT_SECRET", settings.oidc_client_secret),
            )
            if not value.strip()
        ]
        if missing:
            raise RuntimeError(
                "OIDC is enabled but missing required settings: "
                + ", ".join(missing)
            )
    for entry in settings.break_glass_allowed_cidrs.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            raise RuntimeError(
                "BREAK_GLASS_ALLOWED_CIDRS contains an invalid network: "
                f"{candidate}"
            )
    if not settings.bootstrap_admin_username and not settings.bootstrap_admin_password:
        # Valid after first startup: the owner keeps its stored credentials.
        return
    if (
        not settings.bootstrap_admin_username
        or len(settings.bootstrap_admin_password) < 12
        or is_placeholder(
            settings.bootstrap_admin_password,
            "REPLACE-WITH-A-LONG-PASSWORD",
        )
    ):
        raise RuntimeError(
            "For first startup, set BOOTSTRAP_ADMIN_USERNAME and a "
            "BOOTSTRAP_ADMIN_PASSWORD containing at least 12 characters"
        )
