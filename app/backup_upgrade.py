"""CLI: create a pre-upgrade metadata backup, then run Alembic.

Usage:
  python -m app.backup_upgrade
  python -m app.backup_upgrade --revision head

A failed backup exits non-zero and does not run migrations.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import settings, settings_snapshot
from .database import DatabaseSchemaError, db, read_schema_revision
from .migrate_on_start import (
    SchemaMigrationError,
    _alembic_script_directory,
    _validate_local_migration_graph,
    _verify_database_revision_ancestry,
)
from .services import metadata_backups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up encrypted metadata, then run alembic upgrade."
    )
    parser.add_argument(
        "--revision",
        default="head",
        help="Alembic revision target (default: head)",
    )
    parser.add_argument(
        "--skip-upgrade",
        action="store_true",
        help="Only create the pre-upgrade backup",
    )
    args = parser.parse_args(argv)

    # Inspect before even creating a backup/run record or opening S3. A
    # decryptable artifact cannot legitimize an unknown/divergent catalog.
    try:
        with db() as connection:
            revision = read_schema_revision(connection)
        scripts = _alembic_script_directory()
        _validate_local_migration_graph(scripts)
        if revision is not None:
            _verify_database_revision_ancestry(revision)
        target = scripts.get_revision(args.revision)
        if target is None:
            raise SchemaMigrationError("The requested schema target is not supported.")
    except (DatabaseSchemaError, SchemaMigrationError) as exc:
        print(f"Schema upgrade blocked: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Driver/graph failures can contain DSN details. Never echo them.
        print("Schema upgrade blocked: unable to validate catalog and migration target.", file=sys.stderr)
        return 2

    if revision is None:
        if args.skip_upgrade:
            print("Pre-upgrade backup blocked: the database has no catalog to back up.", file=sys.stderr)
            return 2
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", args.revision], check=False,
        ).returncode

    backup_dir = Path(settings.metadata_backup_dir)
    try:
        with db() as connection:
            result = metadata_backups.run_pre_upgrade_backup(
                backup_dir=backup_dir,
                object_store=metadata_backups.default_object_store(),
                retention=settings_snapshot(settings).metadata_backup_retention,
                connection=connection,
            )
    except metadata_backups.BackupError as exc:
        print(f"Pre-upgrade metadata backup failed: {exc}", file=sys.stderr)
        print(
            "Schema upgrade blocked. Fix the backup failure and retry.",
            file=sys.stderr,
        )
        return 2

    print(
        "Pre-upgrade backup ok:",
        result.get("path"),
        f"digest={result.get('digest_sha256')}",
    )
    if args.skip_upgrade:
        return 0

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", args.revision],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
