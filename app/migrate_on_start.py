"""Bring the database schema to HEAD before the application serves traffic.

Used by the container entrypoint (when starting uvicorn) and by the FastAPI
lifespan so native runs behave the same way. Disable with AUTO_MIGRATE=0.

Before automatic migration, an existing database revision must be a known
ancestor of this build's sole Alembic head. Only then can the typed pre-upgrade
backup gate run: configured off-host storage is mandatory and fails closed,
while explicitly local-only development remains an intentional state.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RangeNotAncestorError, ResolutionError
from alembic.util.exc import CommandError

from .config import settings
from .database import HEAD_SCHEMA_REVISION, DatabaseSchemaError, db, read_schema_revision
from .services import metadata_backups
from .system_settings import effective_settings


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class SchemaMigrationError(RuntimeError):
    """Raised when an automatic schema upgrade cannot complete."""


def _alembic_script_directory() -> ScriptDirectory:
    """Load this checkout's migration graph independently of the process cwd."""
    config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _manual_recovery_guidance() -> str:
    return (
        "Automatic migration is blocked. Deploy an application build compatible "
        "with the database revision or restore a compatible metadata backup; "
        "set AUTO_MIGRATE=0 for a deliberate manual recovery."
    )


def _validate_local_migration_graph(scripts: ScriptDirectory) -> None:
    """Require the code checkout to have one head matching its schema constant."""
    try:
        heads = tuple(scripts.get_heads())
    except Exception:  # noqa: BLE001 - never run Alembic on an unreadable graph
        raise SchemaMigrationError(
            "Unable to inspect the local Alembic migration graph. "
            + _manual_recovery_guidance()
        ) from None
    if len(heads) != 1:
        raise SchemaMigrationError(
            "The local Alembic migration graph is divergent. "
            + _manual_recovery_guidance()
        )
    if heads[0] != HEAD_SCHEMA_REVISION:
        raise SchemaMigrationError(
            "The local Alembic head does not match this application's expected "
            "schema revision. "
            + _manual_recovery_guidance()
        )


def _verify_single_database_revision(connection: object, revision: str | None) -> None:
    """Reject Alembic's multi-head database state before any backup work."""
    if revision is None:
        return
    try:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except Exception:  # noqa: BLE001 - do not surface DB/driver details at startup
        raise SchemaMigrationError(
            "Unable to inspect the database Alembic revision state. "
            + _manual_recovery_guidance()
        ) from None
    if len(rows) != 1:
        raise SchemaMigrationError(
            "The database Alembic revision state is divergent. "
            + _manual_recovery_guidance()
        )


def _verify_database_revision_ancestry(revision: str) -> None:
    """Prove ``revision`` is a known ancestor of this build's Alembic head."""
    scripts = _alembic_script_directory()
    _validate_local_migration_graph(scripts)
    try:
        current = scripts.get_revision(revision)
    except (CommandError, ResolutionError):
        raise SchemaMigrationError(
            "The database schema revision is unknown to this application build. "
            + _manual_recovery_guidance()
        ) from None
    if current is None:
        raise SchemaMigrationError(
            "The database schema revision is unknown to this application build. "
            + _manual_recovery_guidance()
        )
    if current.revision == HEAD_SCHEMA_REVISION:
        return

    try:
        # Alembic raises RangeNotAncestorError rather than guessing a direction.
        tuple(scripts.iterate_revisions(HEAD_SCHEMA_REVISION, current.revision))
        return
    except RangeNotAncestorError:
        pass
    except (CommandError, ResolutionError):
        raise SchemaMigrationError(
            "The database schema revision is unknown to this application build. "
            + _manual_recovery_guidance()
        ) from None

    try:
        # A known descendant means the database was migrated by a newer build.
        tuple(scripts.iterate_revisions(current.revision, HEAD_SCHEMA_REVISION))
    except RangeNotAncestorError:
        raise SchemaMigrationError(
            "The database schema revision diverges from this application's "
            "Alembic migration graph. "
            + _manual_recovery_guidance()
        ) from None
    except (CommandError, ResolutionError):
        raise SchemaMigrationError(
            "The database schema revision is unknown to this application build. "
            + _manual_recovery_guidance()
        ) from None
    raise SchemaMigrationError(
        "The database schema is ahead of this application build. "
        + _manual_recovery_guidance()
    )


def _alembic_upgrade(revision: str = "head") -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        check=False,
    )
    if completed.returncode != 0:
        raise SchemaMigrationError(
            f"alembic upgrade {revision} failed with exit code {completed.returncode}"
        )


def _persist_pre_upgrade_backup_failure(
    block_reason: metadata_backups.PreUpgradeBackupBlockReason,
) -> None:
    """Commit failure evidence after the attempted backup transaction rolls back.

    Recording and notification use fresh transactions. The failed backup's
    original ``db()`` context has already rolled back by the time this helper
    runs, so these writes cannot commit unrelated pre-upgrade work.
    """
    error_message = metadata_backups.pre_upgrade_backup_failure_message(
        block_reason
    )
    try:
        with db() as connection:
            metadata_backups.record_pre_upgrade_backup_failure(
                connection,
                backend=settings.db_backend,
                block_reason=block_reason,
            )
    except Exception:
        # Failure evidence must never make an unsafe startup failure look like
        # a successful migration. The primary exception remains fail-closed.
        return

    try:
        with db() as connection:
            metadata_backups.notify_admins_of_backup_failure(
                connection,
                reason="pre_upgrade",
                error_message=error_message,
            )
    except Exception:
        # Notification is best-effort, after the durable run record commits.
        return


def _upgrade_existing_with_backup() -> None:
    backup_dir = Path(settings.metadata_backup_dir)
    try:
        with db() as connection:
            runtime = effective_settings(connection, settings_obj=settings)
            outcome = metadata_backups.run_pre_upgrade_backup_gate(
                backup_dir=backup_dir,
                settings_obj=settings,
                db_backend=settings.db_backend,
                sqlite_path=settings.sqlite_path,
                retention=runtime.metadata_backup_retention,
                s3_prefix=runtime.metadata_backup_s3_prefix,
                connection=connection,
            )
    except metadata_backups.PreUpgradeBackupBlockedError as exc:
        _persist_pre_upgrade_backup_failure(exc.reason)
        raise SchemaMigrationError(
            f"{exc}. Schema upgrade blocked. " + _manual_recovery_guidance()
        ) from None
    except metadata_backups.BackupError:
        # The typed gate should normalize all expected backup failures. Keep an
        # unexpected backup failure fail-closed without exposing its raw detail.
        _persist_pre_upgrade_backup_failure(
            metadata_backups.PreUpgradeBackupBlockReason.BACKUP_FAILED
        )
        raise SchemaMigrationError(
            "Pre-upgrade metadata backup failed. Schema upgrade blocked. "
            + _manual_recovery_guidance()
        ) from None
    except Exception:  # noqa: BLE001 - settings/driver errors must not leak at startup
        _persist_pre_upgrade_backup_failure(
            metadata_backups.PreUpgradeBackupBlockReason.BACKUP_FAILED
        )
        raise SchemaMigrationError(
            "Unable to complete the pre-upgrade backup gate. Schema upgrade blocked. "
            + _manual_recovery_guidance()
        ) from None

    if outcome.state is metadata_backups.PreUpgradeBackupState.OFF_HOST_SUCCEEDED:
        print(
            "AUTO_MIGRATE: pre-upgrade off-host backup ok:",
            outcome.backup.get("path") if outcome.backup else None,
            f"digest={outcome.backup.get('digest_sha256') if outcome.backup else None}",
        )
    elif outcome.backup is not None:
        print(
            "AUTO_MIGRATE: pre-upgrade local-only backup ok:",
            outcome.backup.get("path"),
            f"digest={outcome.backup.get('digest_sha256')}",
        )
    else:
        print(
            "AUTO_MIGRATE: off-host backups are unconfigured and "
            "ARCHIVE_MASTER_KEY is unavailable; local-only development upgrade "
            "is allowed.",
            file=sys.stderr,
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

    try:
        with db() as connection:
            revision = read_schema_revision(connection)
            _verify_single_database_revision(connection, revision)
    except SchemaMigrationError:
        raise
    except Exception:  # noqa: BLE001 - do not expose database/driver details at startup
        raise SchemaMigrationError(
            "Unable to inspect the database schema state. "
            + _manual_recovery_guidance()
        ) from None

    if revision is None:
        # Fresh databases have no ancestry to prove, but the local graph must
        # still be coherent before any Alembic mutation is executed.
        _validate_local_migration_graph(_alembic_script_directory())
        print("AUTO_MIGRATE: unversioned database — running alembic upgrade head")
        _alembic_upgrade()
        return "bootstrapped"

    # Prove direction before creating a backup or asking Alembic to mutate the
    # database. Unknown, ahead, and divergent revisions are never candidates
    # for automatic upgrades.
    _verify_database_revision_ancestry(revision)
    if revision == HEAD_SCHEMA_REVISION:
        print(f"AUTO_MIGRATE: schema already at {HEAD_SCHEMA_REVISION}")
        return "current"

    print(
        f"AUTO_MIGRATE: schema at {revision}, expected {HEAD_SCHEMA_REVISION} — "
        "backing up then upgrading"
    )
    _upgrade_existing_with_backup()

    try:
        with db() as connection:
            after = read_schema_revision(connection)
    except Exception:  # noqa: BLE001 - do not expose database/driver details at startup
        raise DatabaseSchemaError(
            "Automatic migration finished but the database schema could not be verified. "
            + _manual_recovery_guidance()
        ) from None
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
