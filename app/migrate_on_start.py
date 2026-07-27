"""Bring the database schema to HEAD before the application serves traffic.

Used by the container entrypoint (when starting uvicorn) and by the FastAPI
lifespan so native runs behave the same way. Disable with AUTO_MIGRATE=0.

Fresh / unversioned databases run ``alembic upgrade head`` directly. Existing
databases that are behind HEAD take the pre-upgrade backup gate
(``app.backup_upgrade`` / ``run_pre_upgrade_backup``) before Alembic, matching
docs/metadata-backups.md. When backup credentials are clearly not configured
(placeholder AWS / missing ARCHIVE_MASTER_KEY), the upgrade still proceeds
after a warning so local and first-boot installs are not blocked.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import is_placeholder, settings
from .database import HEAD_SCHEMA_REVISION, DatabaseSchemaError, db, read_schema_revision
from .services import metadata_backups
from .system_settings import effective_settings


class SchemaMigrationError(RuntimeError):
    """Raised when an automatic schema upgrade cannot complete."""


def _alembic_upgrade(revision: str = "head") -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        check=False,
    )
    if completed.returncode != 0:
        raise SchemaMigrationError(
            f"alembic upgrade {revision} failed with exit code {completed.returncode}"
        )


def _backup_gate_skippable(exc: BaseException) -> bool:
    """True when backup cannot run because secrets/AWS are unset placeholders."""
    message = str(exc).lower()
    needles = (
        "archive_master_key is not configured",
        "archive_master_key must be",
        "aws credentials are not configured",
        "s3 bucket name is not configured",
        "configured metadata backup object store is unavailable",
    )
    return any(needle in message for needle in needles)


def _object_store_for_upgrade() -> metadata_backups.ObjectStore | None:
    bucket = (settings.vault_s3_bucket or "").strip()
    if not bucket or is_placeholder(bucket, "example-bucket"):
        return None
    try:
        return metadata_backups.default_object_store()
    except metadata_backups.BackupError as exc:
        if _backup_gate_skippable(exc):
            print(
                f"AUTO_MIGRATE: off-host backup skipped ({exc})",
                file=sys.stderr,
            )
            return None
        raise


def _upgrade_existing_with_backup() -> None:
    backup_dir = Path(settings.metadata_backup_dir)
    try:
        with db() as connection:
            runtime = effective_settings(connection, settings_obj=settings)
            result = metadata_backups.run_pre_upgrade_backup(
                backup_dir=backup_dir,
                object_store=_object_store_for_upgrade(),
                retention=runtime.metadata_backup_retention,
                connection=connection,
            )
    except metadata_backups.BackupError as exc:
        if _backup_gate_skippable(exc):
            print(
                "AUTO_MIGRATE: pre-upgrade backup unavailable "
                f"({exc}); upgrading without backup. "
                "Set ARCHIVE_MASTER_KEY (and real AWS when using S3 backups) "
                "for production upgrades.",
                file=sys.stderr,
            )
            _alembic_upgrade()
            return
        raise SchemaMigrationError(
            f"Pre-upgrade metadata backup failed: {exc}. "
            "Schema upgrade blocked. Fix the backup failure, or set "
            "AUTO_MIGRATE=0 and upgrade manually after "
            "`python -m app.backup_upgrade`."
        ) from exc

    print(
        "AUTO_MIGRATE: pre-upgrade backup ok:",
        result.get("path"),
        f"digest={result.get('digest_sha256')}",
    )
    _alembic_upgrade()


def ensure_schema_current() -> str:
    """Ensure the live schema matches ``HEAD_SCHEMA_REVISION``.

    Returns a short status token: ``disabled``, ``current``, ``bootstrapped``,
    or ``upgraded``. Raises ``SchemaMigrationError`` / ``DatabaseSchemaError``
    when the schema cannot be made current.
    """
    if not settings.auto_migrate:
        return "disabled"

    with db() as connection:
        revision = read_schema_revision(connection)

    if revision == HEAD_SCHEMA_REVISION:
        print(f"AUTO_MIGRATE: schema already at {HEAD_SCHEMA_REVISION}")
        return "current"

    if revision is None:
        print("AUTO_MIGRATE: unversioned database — running alembic upgrade head")
        _alembic_upgrade()
        return "bootstrapped"

    # Known revision that is not HEAD: only upgrade forward. Never auto-downgrade.
    print(
        f"AUTO_MIGRATE: schema at {revision}, expected {HEAD_SCHEMA_REVISION} — "
        "backing up then upgrading"
    )
    _upgrade_existing_with_backup()

    with db() as connection:
        after = read_schema_revision(connection)
    if after != HEAD_SCHEMA_REVISION:
        raise DatabaseSchemaError(
            "Automatic migration finished but schema is still not current "
            f"(found {after or 'unversioned'}, expected {HEAD_SCHEMA_REVISION})."
        )
    return "upgraded"


def main(argv: list[str] | None = None) -> int:
    del argv  # CLI has no flags today; kept for symmetry with other modules.
    try:
        status = ensure_schema_current()
    except (SchemaMigrationError, DatabaseSchemaError, metadata_backups.BackupError) as exc:
        print(f"AUTO_MIGRATE failed: {exc}", file=sys.stderr)
        return 2
    print(f"AUTO_MIGRATE: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
