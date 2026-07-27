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

from .config import settings
from .database import db
from .services import metadata_backups
from .system_settings import effective_settings


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

    backup_dir = Path(settings.metadata_backup_dir)
    try:
        with db() as connection:
            runtime = effective_settings(connection, settings_obj=settings)
            result = metadata_backups.run_pre_upgrade_backup(
                backup_dir=backup_dir,
                object_store=metadata_backups.default_object_store(),
                retention=runtime.metadata_backup_retention,
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
