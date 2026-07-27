from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import Settings, is_placeholder, settings


class InvalidSystemSetting(ValueError):
    pass


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    environment_variable: str
    group: str
    value_type: type
    built_in_default: Any
    mutability: str
    restart_required: bool = False
    secret: bool = False
    settings_attribute: str | None = None
    environment_aliases: tuple[str, ...] = ()
    environment_fallbacks: tuple[str, ...] = ()
    nullable: bool = False


@dataclass(frozen=True)
class ResolvedSystemSetting:
    definition: SettingDefinition
    value: Any
    source: str


_DEFAULT_ATTRIBUTE = object()


def _setting(
    key: str,
    env: str,
    group: str,
    value_type: type,
    default: Any,
    mutability: str,
    *,
    restart: bool = False,
    secret: bool = False,
    attribute: str | None | object = _DEFAULT_ATTRIBUTE,
    aliases: tuple[str, ...] = (),
    fallbacks: tuple[str, ...] = (),
    nullable: bool = False,
) -> SettingDefinition:
    settings_attribute = key if attribute is _DEFAULT_ATTRIBUTE else attribute
    if settings_attribute is not None and not isinstance(settings_attribute, str):
        raise TypeError("settings attribute must be a string or None")
    return SettingDefinition(
        key=key,
        environment_variable=env,
        group=group,
        value_type=value_type,
        built_in_default=default,
        mutability=mutability,
        restart_required=restart,
        secret=secret,
        settings_attribute=settings_attribute,
        environment_aliases=aliases,
        environment_fallbacks=fallbacks,
        nullable=nullable,
    )


_D = "deployment_only"
_M = "runtime_managed"
_R = "restart_required"

SETTING_DEFINITIONS = (
    _setting("db_backend", "DB_BACKEND", "operations", str, "postgresql", _D, restart=True),
    _setting("sqlite_path", "SQLITE_PATH", "operations", str, "/data/frostvault.db", _D, restart=True),
    _setting("database_host", "PGHOST", "operations", str, "localhost", _D, restart=True, attribute=None),
    _setting("database_port", "PGPORT", "operations", int, 5432, _D, restart=True, attribute=None),
    # migrations/env.py defaults PGUSER to postgres, then PGDATABASE to that user.
    _setting(
        "database_name",
        "PGDATABASE",
        "operations",
        str,
        "postgres",
        _D,
        restart=True,
        attribute=None,
        fallbacks=("PGUSER",),
    ),
    _setting("database_user", "PGUSER", "operations", str, "postgres", _D, restart=True, attribute=None),
    _setting("database_password", "PGPASSWORD", "operations", str, "", _D, restart=True, secret=True, attribute=None),
    _setting("session_cookie_name", "SESSION_COOKIE_NAME", "security", str, "frostvault_session", _D, restart=True),
    _setting("csrf_cookie_name", "CSRF_COOKIE_NAME", "security", str, "frostvault_csrf", _D, restart=True),
    _setting("session_idle_seconds", "SESSION_IDLE_SECONDS", "security", int, 43200, _R, restart=True),
    _setting("session_absolute_seconds", "SESSION_ABSOLUTE_SECONDS", "security", int, 604800, _R, restart=True),
    _setting("cookie_secure", "COOKIE_SECURE", "security", bool, False, _D, restart=True),
    _setting("allowed_hosts", "ALLOWED_HOSTS", "security", str, "", _D, restart=True),
    _setting("trusted_proxies", "TRUSTED_PROXIES", "security", str, "", _D, restart=True),
    _setting("reauth_window_seconds", "REAUTH_WINDOW_SECONDS", "security", int, 600, _M),
    _setting("break_glass_allowed_cidrs", "BREAK_GLASS_ALLOWED_CIDRS", "security", str, "", _D, restart=True),
    _setting("rclone_config", "RCLONE_CONFIG", "operations", str, "/config/rclone/rclone.conf", _D, restart=True),
    _setting("aws_region", "AWS_DEFAULT_REGION", "operations", str, "eu-south-1", _D, restart=True),
    _setting("aws_access_key_id", "AWS_ACCESS_KEY_ID", "operations", str, "", _D, restart=True, secret=True, attribute=None),
    _setting("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "operations", str, "", _D, restart=True, secret=True, attribute=None),
    _setting("aws_session_token", "AWS_SESSION_TOKEN", "operations", str, "", _D, restart=True, secret=True, attribute=None),
    _setting("scan_interval", "SCAN_INTERVAL_SECONDS", "operations", int, 21600, _M),
    _setting("audit_interval", "AUDIT_INTERVAL_SECONDS", "operations", int, 604800, _M),
    _setting("filesystem_watch_enabled", "FILESYSTEM_WATCH_ENABLED", "operations", bool, True, _R, restart=True),
    _setting("filesystem_watch_force_polling", "FILESYSTEM_WATCH_FORCE_POLLING", "operations", bool, False, _R, restart=True),
    _setting("filesystem_watch_debounce_ms", "FILESYSTEM_WATCH_DEBOUNCE_MS", "operations", int, 1200, _M),
    _setting("filesystem_watch_poll_ms", "FILESYSTEM_WATCH_POLL_MS", "operations", int, 2000, _M),
    _setting("queue_poll_interval", "QUEUE_POLL_SECONDS", "operations", int, 5, _M),
    _setting("operation_concurrency", "OPERATION_CONCURRENCY", "operations", int, 4, _M, aliases=("UPLOAD_CONCURRENCY",)),
    _setting("cloud_purge_delay_seconds", "CLOUD_PURGE_DELAY_SECONDS", "operations", int, 86400, _M),
    _setting("allow_local_delete", "ALLOW_LOCAL_DELETE", "operations", bool, False, _M),
    _setting("bandwidth_limit_kibps", "BANDWIDTH_LIMIT_KIBPS", "operations", int, None, _M, nullable=True),
    _setting("s3_download_max_concurrency", "S3_DOWNLOAD_MAX_CONCURRENCY", "operations", int, 10, _M),
    _setting("s3_download_multipart_threshold_mib", "S3_DOWNLOAD_MULTIPART_THRESHOLD_MIB", "operations", int, 8, _M),
    _setting("s3_download_multipart_chunksize_mib", "S3_DOWNLOAD_MULTIPART_CHUNKSIZE_MIB", "operations", int, 8, _M),
    _setting("rclone_multi_thread_streams", "RCLONE_MULTI_THREAD_STREAMS", "operations", int, 8, _M),
    _setting("rclone_multi_thread_cutoff_mib", "RCLONE_MULTI_THREAD_CUTOFF_MIB", "operations", int, 64, _M),
    _setting("job_progress_min_interval_ms", "JOB_PROGRESS_MIN_INTERVAL_MS", "operations", int, 500, _M),
    _setting("restore_poll_interval", "JOB_POLL_SECONDS", "restore", int, 900, _M),
    _setting("restore_days", "RESTORE_DAYS", "restore", int, 3, _M),
    _setting("restore_tier", "RESTORE_TIER", "restore", str, "Bulk", _M),
    _setting("restore_high_impact_gib", "RESTORE_HIGH_IMPACT_GIB", "restore", float, 100.0, _M),
    _setting("restore_high_impact_eur", "RESTORE_HIGH_IMPACT_EUR", "restore", float, 10.0, _M),
    _setting("restore_approval_hold_seconds", "RESTORE_APPROVAL_HOLD_SECONDS", "restore", int, 3600, _M),
    _setting("oidc_enabled", "OIDC_ENABLED", "oidc", bool, False, _R, restart=True),
    _setting("oidc_issuer", "OIDC_ISSUER", "oidc", str, "", _R, restart=True),
    _setting("oidc_client_id", "OIDC_CLIENT_ID", "oidc", str, "", _R, restart=True),
    _setting("oidc_client_secret", "OIDC_CLIENT_SECRET", "oidc", str, "", _D, restart=True, secret=True),
    _setting("oidc_scopes", "OIDC_SCOPES", "oidc", str, "openid email profile", _R, restart=True),
    _setting("oidc_login_ttl_seconds", "OIDC_LOGIN_TTL_SECONDS", "oidc", int, 600, _R, restart=True),
    _setting("invite_ttl_seconds", "INVITE_TTL_SECONDS", "security", int, 604800, _M),
    _setting("bootstrap_admin_username", "BOOTSTRAP_ADMIN_USERNAME", "security", str, "", _D, restart=True),
    _setting("bootstrap_admin_password", "BOOTSTRAP_ADMIN_PASSWORD", "security", str, "", _D, restart=True, secret=True),
    _setting("bootstrap_admin_display_name", "BOOTSTRAP_ADMIN_DISPLAY_NAME", "security", str, "Administrator", _D, restart=True),
    _setting("bootstrap_vault_name", "BOOTSTRAP_VAULT_NAME", "vault_defaults", str, "", _D, restart=True),
    _setting("bootstrap_vault_slug", "BOOTSTRAP_VAULT_SLUG", "vault_defaults", str, "", _D, restart=True),
    _setting("bootstrap_vault_source_root", "BOOTSTRAP_VAULT_SOURCE_ROOT", "vault_defaults", str, "", _D, restart=True),
    _setting("bootstrap_vault_s3_bucket", "S3_BUCKET", "vault_defaults", str, "", _D, restart=True),
    _setting("bootstrap_vault_s3_prefix", "BOOTSTRAP_VAULT_S3_PREFIX", "vault_defaults", str, "", _D, restart=True),
    _setting("bootstrap_vault_rclone_remote", "BOOTSTRAP_VAULT_RCLONE_REMOTE", "vault_defaults", str, "", _D, restart=True),
    _setting("vault_sources_root", "VAULT_SOURCES_ROOT", "vault_defaults", str, "/sources", _D, restart=True),
    _setting("vault_s3_bucket", "VAULT_S3_BUCKET", "vault_defaults", str, "", _D, restart=True, aliases=("S3_BUCKET",)),
    _setting("vault_rclone_remote", "VAULT_RCLONE_REMOTE", "vault_defaults", str, "", _D, restart=True),
    _setting("vault_rclone_base_remote", "VAULT_RCLONE_BASE_REMOTE", "vault_defaults", str, "", _D, restart=True),
    _setting("archive_master_key", "ARCHIVE_MASTER_KEY", "security", str, "", _D, restart=True, secret=True),
    _setting("auto_migrate", "AUTO_MIGRATE", "operations", bool, True, _D, restart=True),
    _setting("metadata_backup_dir", "METADATA_BACKUP_DIR", "operations", str, "/data/backups", _D, restart=True),
    _setting("metadata_backup_retention", "METADATA_BACKUP_RETENTION", "operations", int, 14, _M),
    _setting("metadata_backup_interval_seconds", "METADATA_BACKUP_INTERVAL_SECONDS", "operations", int, 86400, _M),
    _setting("metadata_backup_s3_prefix", "METADATA_BACKUP_S3_PREFIX", "operations", str, "system/backups/", _M),
    _setting("metadata_backup_verify_interval_seconds", "METADATA_BACKUP_VERIFY_INTERVAL_SECONDS", "operations", int, 604800, _M),
    _setting("frontend_dist_dir", "FRONTEND_DIST_DIR", "operations", str, "", _D, restart=True),
    _setting("vapid_public_key", "VAPID_PUBLIC_KEY", "operations", str, "", _D, restart=True),
    _setting("vapid_private_key", "VAPID_PRIVATE_KEY", "operations", str, "", _D, restart=True, secret=True),
    _setting("vapid_subject", "VAPID_SUBJECT", "operations", str, "mailto:admin@localhost", _D, restart=True),
)

SETTINGS_BY_KEY = {item.key: item for item in SETTING_DEFINITIONS}


def _valid_value(definition: SettingDefinition, value: Any) -> bool:
    if value is None:
        return definition.nullable
    if definition.value_type is bool:
        return type(value) is bool
    if definition.value_type is int:
        return type(value) is int
    if definition.value_type is float:
        return type(value) in (int, float)
    return type(value) is definition.value_type


def _decode_value(raw: Any, *, sqlite: bool) -> Any:
    return json.loads(raw) if sqlite and isinstance(raw, str) else raw


def _environment_value(
    definition: SettingDefinition,
    settings_obj: Settings,
    environ: Mapping[str, str],
) -> Any:
    if definition.settings_attribute:
        return getattr(settings_obj, definition.settings_attribute)
    raw = environ.get(definition.environment_variable)
    if raw is None:
        raw = next(
            (
                environ[alias]
                for alias in (
                    *definition.environment_aliases,
                    *definition.environment_fallbacks,
                )
                if alias in environ
            ),
            None,
        )
    if raw is None:
        return definition.built_in_default
    if definition.value_type is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if definition.value_type is int:
        return int(raw)
    if definition.value_type is float:
        return float(raw)
    return raw


def resolve_system_settings(
    connection: Any | None,
    *,
    settings_obj: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, ResolvedSystemSetting]:
    configured = settings_obj or settings
    environment = environ if environ is not None else os.environ
    rows: list[dict[str, Any]] = []
    sqlite = getattr(connection, "backend", None) == "sqlite"
    if connection is not None:
        if sqlite:
            present = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='system_settings'"
            ).fetchone()
        else:
            present = connection.execute(
                "SELECT to_regclass('public.system_settings') AS name"
            ).fetchone()
        if present and present["name"]:
            rows = connection.execute("SELECT key, value FROM system_settings").fetchall()
    overrides = {
        row["key"]: _decode_value(row["value"], sqlite=sqlite)
        for row in rows
    }
    resolved: dict[str, ResolvedSystemSetting] = {}
    for definition in SETTING_DEFINITIONS:
        if definition.key in overrides:
            value = overrides[definition.key]
            if not _valid_value(definition, value):
                raise InvalidSystemSetting(
                    f"Persisted value for {definition.key} has an invalid type"
                )
            source = "database_override"
        else:
            env_names = (
                definition.environment_variable,
                *definition.environment_aliases,
                *definition.environment_fallbacks,
            )
            source = (
                "environment_default"
                if any(name in environment for name in env_names)
                else "built_in_default"
            )
            value = _environment_value(definition, configured, environment)
        resolved[definition.key] = ResolvedSystemSetting(definition, value, source)
    return resolved


def system_settings_response(
    connection: Any,
    *,
    settings_obj: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {
        "security": [],
        "oidc": [],
        "operations": [],
        "restore": [],
        "vault_defaults": [],
    }
    for resolved in resolve_system_settings(
        connection,
        settings_obj=settings_obj,
        environ=environ,
    ).values():
        definition = resolved.definition
        item: dict[str, Any] = {
            "key": definition.key,
            "environment_variable": definition.environment_variable,
            "source": resolved.source,
            "mutability": definition.mutability,
            "restart_required": definition.restart_required,
        }
        if definition.secret:
            value = str(resolved.value or "").strip()
            item["configured"] = bool(value) and not is_placeholder(value)
        else:
            item["effective_value"] = resolved.value
        groups[definition.group].append(item)
    return {"groups": groups}


def set_system_setting(
    connection: Any,
    *,
    key: str,
    value: Any,
    updated_by: int,
) -> None:
    definition = SETTINGS_BY_KEY.get(key)
    if definition is None:
        raise InvalidSystemSetting(f"Unknown system setting: {key}")
    if definition.mutability != _M or definition.secret:
        raise InvalidSystemSetting(f"System setting is not runtime-managed: {key}")
    if not _valid_value(definition, value):
        raise InvalidSystemSetting(f"Invalid value type for system setting: {key}")
    if definition.value_type is float and value is not None:
        value = float(value)
    connection.execute(
        """
        INSERT INTO system_settings(key, value, updated_by, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (key) DO UPDATE SET
            value=excluded.value,
            updated_by=excluded.updated_by,
            updated_at=excluded.updated_at
        """,
        (
            key,
            json.dumps(value),
            updated_by,
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        ),
    )
