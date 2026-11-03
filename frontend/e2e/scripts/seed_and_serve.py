#!/usr/bin/env python3
"""Seed a PostgreSQL FrostVault and serve the SPA for Playwright e2e (issue #70).

Creates the single Owner (Emergency Password sign-in plus a pre-minted Session
cookie), two vaults, and a small catalog tree with Path History. Then runs uvicorn serving the built SPA.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = Path(__file__).resolve().parents[1] / ".runtime"
CREDENTIALS_PATH = RUNTIME / "credentials.json"

PASSWORD = "e2e-test-password-ok"
OWNER_USERNAME = "e2e-owner"


def _ensure_runtime() -> dict[str, Path]:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    source_primary = RUNTIME / "sources" / "family"
    source_secondary = RUNTIME / "sources" / "secondary"
    source_primary.mkdir(parents=True, exist_ok=True)
    source_secondary.mkdir(parents=True, exist_ok=True)
    (source_primary / "reports").mkdir(exist_ok=True)
    (source_primary / "reports" / "readme.txt").write_text(
        "family reports readme\n", encoding="utf-8"
    )
    (source_primary / "note.txt").write_text("a local note\n", encoding="utf-8")
    (source_secondary / "hello.txt").write_text("secondary\n", encoding="utf-8")
    db_path = RUNTIME / "frostvault-e2e.db"
    # This path is an opaque identity for a uniquely named disposable database.
    return {
        "db": db_path,
        "source_primary": source_primary,
        "source_secondary": source_secondary,
    }


def _migrate(db_path: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from tests.postgres import run_alembic, database_dsn
    result = run_alembic(db_path)
    os.environ["DATABASE_URL"] = database_dsn(db_path)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"alembic upgrade failed: {result.returncode}")


def _seed(paths: dict[str, Path]) -> dict:
    # Import after the isolated PostgreSQL DSN and filesystem env are prepared.
    sys.path.insert(0, str(REPO_ROOT))
    from app.catalog import ArchiveCatalog
    from app.database import db
    from app.security import hash_password
    from app.sessions import create_session, csrf_token_for

    password_hash = hash_password(PASSWORD)
    with db() as connection:
        owner_id = connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (OWNER_USERNAME, "E2E Owner", password_hash),
        ).fetchone()["id"]

        primary_id = connection.execute(
            """
            INSERT INTO vaults(
                slug, name, source_root, s3_bucket, s3_prefix,
                cloud_deletion_enabled
            ) VALUES (
                'family', 'Family Archive', %s, 'e2e-bucket', 'family', TRUE
            ) RETURNING id
            """,
            (str(paths["source_primary"]),),
        ).fetchone()["id"]
        secondary_id = connection.execute(
            """
            INSERT INTO vaults(
                slug, name, source_root, s3_bucket, s3_prefix) VALUES (
                'secondary', 'Secondary Archive', %s, 'e2e-bucket',
                'secondary') RETURNING id
            """,
            (str(paths["source_secondary"]),),
        ).fetchone()["id"]

        catalog = ArchiveCatalog(connection)
        readme_id = catalog.observe_local_copy(
            vault_id=primary_id,
            path="reports/old-readme.txt",
            file_type="regular",
            size=21,
            mtime_ns=1_700_000_000_000_000_000,
            observed_at="2026-07-01T10:00:00+00:00",
        )
        catalog.rename_file(
            readme_id,
            new_path="reports/readme.txt",
            changed_at="2026-07-01T12:00:00+00:00",
            vault_id=primary_id,
        )
        catalog.record_archive_version(
            vault_id=primary_id,
            path="reports/readme.txt",
            object_key="family/reports/readme.txt",
            provider_version_id="v-readme-1",
            size=21,
            storage_class="STANDARD",
            etag="etag-readme",
            uploaded_at="2026-07-01T11:00:00+00:00",
            observed_at="2026-07-01T11:00:00+00:00",
            scan_id="seed-scan",
        )
        catalog.observe_local_copy(
            vault_id=primary_id,
            path="note.txt",
            file_type="regular",
            size=12,
            mtime_ns=1_700_000_000_100_000_000,
            observed_at="2026-07-02T10:00:00+00:00",
        )
        note_version_id = catalog.record_archive_version(
            vault_id=primary_id,
            path="note.txt",
            object_key="family/note.txt",
            provider_version_id="v-note-1",
            size=12,
            storage_class="STANDARD",
            etag="etag-note",
            uploaded_at="2026-07-02T11:00:00+00:00",
            observed_at="2026-07-02T11:00:00+00:00",
            scan_id="seed-scan",
        )
        note_digest = "a" * 64
        catalog.mark_version_verified(
            note_version_id,
            plaintext_sha256=note_digest,
            verified_at="2026-07-02T12:00:00+00:00",
        )
        catalog.set_local_fingerprint(
            vault_id=primary_id,
            path="note.txt",
            plaintext_sha256=note_digest,
            matched_archive_version_id=note_version_id,
        )
        catalog.observe_local_copy(
            vault_id=secondary_id,
            path="hello.txt",
            file_type="regular",
            size=10,
            mtime_ns=1_700_000_000_200_000_000,
            observed_at="2026-07-03T10:00:00+00:00",
        )

        owner_token = create_session(
            connection, user_id=owner_id, auth_method="local"
        )
        connection.execute(
            "UPDATE sessions SET vault_id=%s WHERE user_id=%s",
            (primary_id, owner_id),
        )

        credentials = {
            "password": PASSWORD,
            "owner": {
                "username": OWNER_USERNAME,
                "password": PASSWORD,
                "session": owner_token,
                "csrf": csrf_token_for(connection, owner_token),
            },
            "vaults": {
                "primary": {"id": primary_id, "name": "Family Archive"},
                "secondary": {"id": secondary_id, "name": "Secondary Archive"},
            },
        }

    CREDENTIALS_PATH.write_text(
        json.dumps(credentials, indent=2) + "\n", encoding="utf-8"
    )
    return credentials


def _apply_env(paths: dict[str, Path]) -> None:
    master_key = os.environ.get("ARCHIVE_MASTER_KEY")
    if not master_key:
        from cryptography.fernet import Fernet

        master_key = Fernet.generate_key().decode()
    env = {
        "COOKIE_SECURE": "false",
        "FRONTEND_DIST_DIR": str(REPO_ROOT / "frontend" / "dist"),
        "ALLOW_LOCAL_DELETE": "true",
        "BREAK_GLASS_ALLOWED_CIDRS": "",
        "ARCHIVE_MASTER_KEY": master_key,
        "S3_BUCKET": "e2e-bucket",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "eu-south-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "BOOTSTRAP_ADMIN_USERNAME": "",
        "BOOTSTRAP_ADMIN_PASSWORD": "",
        "SOURCES_ROOT": str(RUNTIME / "sources"),
        "FROSTVAULT_TEST_SOURCES_ROOT": str(RUNTIME / "sources"),
        "FILESYSTEM_WATCH_ENABLED": "false",
        "SCAN_INTERVAL_SECONDS": "86400",
        "AUDIT_INTERVAL_SECONDS": "86400",
        # Maintenance that never ran is due at startup. The seeded bucket is
        # fake, so scheduled metadata backups and their restore verification
        # would only race the flows under test; 0 is the documented off switch.
        "METADATA_BACKUP_INTERVAL_SECONDS": "0",
        "METADATA_BACKUP_VERIFY_INTERVAL_SECONDS": "0",
    }
    os.environ.update(env)


def main() -> None:
    paths = _ensure_runtime()
    _apply_env(paths)
    _migrate(paths["db"])
    _seed(paths)
    # Re-import settings after env is set: uvicorn loads app.main which reads
    # settings at import. Clear cached modules if we already imported them.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    import uvicorn

    host = os.environ.get("E2E_HOST", "127.0.0.1")
    port = int(os.environ.get("E2E_PORT", "8080"))
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
