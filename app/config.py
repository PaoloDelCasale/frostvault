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
    # Global rclone bandwidth cap in KiB/s. None/0 means unlimited; per-vault
    # operation policies may override with a tighter or explicit limit.
    bandwidth_limit_kibps: int | None = (
        None
        if not os.getenv("BANDWIDTH_LIMIT_KIBPS", "").strip()
        else max(0, int(os.getenv("BANDWIDTH_LIMIT_KIBPS", "0")))
    )

    break_glass_allowed_cidrs: str = os.getenv("BREAK_GLASS_ALLOWED_CIDRS", "")

    oidc_enabled: bool = as_bool("OIDC_ENABLED", False)
    oidc_issuer: str = os.getenv("OIDC_ISSUER", "")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "")
    oidc_scopes: str = os.getenv("OIDC_SCOPES", "openid email profile")
    oidc_login_ttl_seconds: int = max(
        60, int(os.getenv("OIDC_LOGIN_TTL_SECONDS", "600"))
    )
    invite_ttl_seconds: int = max(
        300, int(os.getenv("INVITE_TTL_SECONDS", str(7 * 24 * 60 * 60)))
    )

    bootstrap_admin_username: str = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "")
    bootstrap_admin_password: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    bootstrap_admin_display_name: str = os.getenv("BOOTSTRAP_ADMIN_DISPLAY_NAME", "Administrator")
    bootstrap_vault_name: str = os.getenv("BOOTSTRAP_VAULT_NAME", "")
    bootstrap_vault_slug: str = os.getenv("BOOTSTRAP_VAULT_SLUG", "")
    bootstrap_vault_source_root: str = os.getenv("BOOTSTRAP_VAULT_SOURCE_ROOT", "")
    bootstrap_vault_s3_bucket: str = os.getenv("S3_BUCKET", "")
    bootstrap_vault_s3_prefix: str = os.getenv("BOOTSTRAP_VAULT_S3_PREFIX", "")
    bootstrap_vault_rclone_remote: str = os.getenv("BOOTSTRAP_VAULT_RCLONE_REMOTE", "")

    # Self-service vault creation (issue #7): the server -- never the caller
    # -- derives every vault's storage namespace from a generated UUID.
    # vault_sources_root is the in-container root new local directories are
    # created under; vault_s3_bucket/vault_rclone_remote are the shared
    # bucket/remote assigned to self-service plain vaults.
    vault_sources_root: str = os.getenv("VAULT_SOURCES_ROOT", "/sources")
    vault_s3_bucket: str = os.getenv("VAULT_S3_BUCKET", os.getenv("S3_BUCKET", ""))
    vault_rclone_remote: str = os.getenv("VAULT_RCLONE_REMOTE", "")
    # Underlying S3 (or compatible) remote that per-vault crypt remotes wrap
    # at runtime (issue #6). Distinct from VAULT_RCLONE_REMOTE, which may be a
    # shared legacy crypt/alias remote used by plain vaults.
    vault_rclone_base_remote: str = os.getenv("VAULT_RCLONE_BASE_REMOTE", "")
    # Fernet key (url-safe base64-encoded 32 bytes) that seals per-vault crypt
    # secrets at rest. Required to create or operate crypt vaults.
    archive_master_key: str = os.getenv("ARCHIVE_MASTER_KEY", "")

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

    # When true, HTML routes serve the React SPA from frontend_dist_dir
    # instead of Jinja templates (epic #56 / issue #58). Default off keeps
    # today's Jinja behaviour unchanged.
    frontend_spa: bool = as_bool("FRONTEND_SPA", False)
    frontend_dist_dir: str = os.getenv("FRONTEND_DIST_DIR", _DEFAULT_FRONTEND_DIST)


settings = Settings()


def validate_settings() -> None:
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
        # This is valid after first startup, when an administrator already exists.
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
