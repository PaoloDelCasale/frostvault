#!/usr/bin/env python3
"""Seed a PostgreSQL FrostVault and serve the SPA for Playwright e2e (issue #70).

Creates the single Owner (Emergency Password sign-in plus a pre-minted Session
cookie), two vaults, and a small catalog tree with Path History. Then runs uvicorn serving the built SPA.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = Path(__file__).resolve().parents[1] / ".runtime"
CREDENTIALS_PATH = RUNTIME / "credentials.json"

PASSWORD = "e2e-test-password-ok"
OWNER_USERNAME = "e2e-owner"


def _ensure_runtime() -> dict[str, Path]:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Ordinary direct children are not Source Volumes. Use actual managed
    # UUID roots so the same containment/access gate as production is exercised.
    source_primary = RUNTIME / "sources" / "managed" / "00000000-0000-4000-8000-000000000001"
    source_secondary = RUNTIME / "sources" / "managed" / "00000000-0000-4000-8000-000000000002"
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
                slug, name, source_root, s3_bucket, s3_prefix
            ) VALUES (
                'family', 'Family Archive', %s, 'e2e-bucket', 'family'
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
        note_path = paths["source_primary"] / "note.txt"
        note_stat = note_path.stat()
        catalog.observe_local_copy(
            vault_id=primary_id,
            path="note.txt",
            file_type="regular",
            size=note_stat.st_size,
            mtime_ns=note_stat.st_mtime_ns,
            observed_at="2026-07-02T10:00:00+00:00",
        )
        note_version_id = catalog.record_archive_version(
            vault_id=primary_id,
            path="note.txt",
            object_key="family/note.txt",
            provider_version_id="v-note-1",
            size=note_stat.st_size,
            storage_class="STANDARD",
            etag="etag-note",
            uploaded_at="2026-07-02T11:00:00+00:00",
            observed_at="2026-07-02T11:00:00+00:00",
            scan_id="seed-scan",
        )
        note_digest = hashlib.sha256(note_path.read_bytes()).hexdigest()
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

        # Real catalog/API fixtures for the essential interface. S3 transfer
        # execution is covered by the separate MinIO CI job, not this UI server.
        for vault_id, prefix in ((primary_id, 'family'), (secondary_id, 'secondary')):
            version = catalog.record_archive_version(
                vault_id=vault_id, path='photos/cold.jpg', object_key=f'{prefix}/photos/cold.jpg',
                provider_version_id=f'cold-{vault_id}', size=2048, storage_class='DEEP_ARCHIVE',
                etag='cold', uploaded_at='2026-07-01T10:00:00Z', observed_at='2026-07-01T10:00:00Z', scan_id='seed')
            catalog.mark_version_verified(version, plaintext_sha256='a'*64, verified_at='2026-07-01T10:00:00Z')
        conflict_path = paths['source_primary'] / 'incoming' / 'conflict.jpg'
        conflict_path.parent.mkdir(exist_ok=True)
        conflict_path.write_bytes(b'new different local bytes')
        held = catalog.record_archive_version(
            vault_id=primary_id, path='incoming/conflict.jpg', object_key='family/incoming/conflict.jpg',
            provider_version_id='held-only-copy', size=12, storage_class='STANDARD', etag='held',
            uploaded_at='2026-07-01T10:00:00Z', observed_at='2026-07-01T10:00:00Z', scan_id='seed')
        catalog.mark_version_verified(held, plaintext_sha256='b'*64, verified_at='2026-07-01T10:00:00Z')
        conflict_file = catalog.observe_local_copy(vault_id=primary_id, path='incoming/conflict.jpg', file_type='regular', size=conflict_path.stat().st_size, mtime_ns=conflict_path.stat().st_mtime_ns, observed_at='2026-07-01T11:00:00Z')
        connection.execute('UPDATE local_copies SET cloud_only_version_id=%s,plaintext_sha256=%s WHERE vault_file_id=%s', (held, hashlib.sha256(conflict_path.read_bytes()).hexdigest(), conflict_file))
        from app.services.notifications import set_problem
        set_problem(connection, key='conflict-e2e', event='cloud_only_conflict', vault_id=primary_id, active=True, params={'path':'incoming/conflict.jpg'})

        # UI flows use fixed cloud evidence, not a live S3 provider. Suspend
        # automatic admission explicitly; suspension must not revoke protection
        # of the verified note or its manual free-space affordance.
        for vault_id in (primary_id, secondary_id):
            connection.execute(
                "INSERT INTO vault_operation_policies(vault_id, auto_upload) VALUES (%s, FALSE)",
                (vault_id,),
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


@contextmanager
def _native_fixture_runtime():
    """Model only the native mount boundary and a freshly seeded catalog.

    UI tests do not provision Linux mounts or a real S3 bucket. Keep the actual
    source-access, fingerprint and API authorization checks, but represent the
    test sources root as mounted (no arbitrary child gets mount status). Treat
    seeding as the latest maintenance pass so the fake provider is not audited
    and does not race the UI scenarios. Real maintenance/S3 has separate CI jobs.
    """
    from app import storage
    from app.services import source_layout

    real_mount_probe = source_layout.path_is_mount
    sources = source_layout.get_sources_root().resolve()
    real_maintenance_ages = storage.persisted_maintenance_ages

    def fixture_mount(path):
        return Path(path).resolve() == sources or real_mount_probe(path)

    def fixture_maintenance_ages(**kwargs):
        return {**real_maintenance_ages(**kwargs), "scan": 0.0, "audit": 0.0}

    with patch.object(source_layout, "path_is_mount", fixture_mount), patch.object(
        storage, "persisted_maintenance_ages", fixture_maintenance_ages,
    ):
        yield


def _legacy_worker_fixture(application):
    """Serve the old Worker from the CI server, not Playwright's page routes.

    Chromium fetches a Worker's entry script outside page interception. Serving
    the production retirement script here can unregister before `ready` resolves.
    Only this disposable E2E app recognizes the exact fixture query string.
    """
    async def fixture(scope, receive, send):
        if (scope["type"] == "http" and scope["method"] == "GET"
                and scope["path"] == "/sw.js" and scope.get("query_string") == b"legacy-test"):
            from starlette.responses import Response
            response = Response(
                '// legacy-worker-fixture\n'
                'self.addEventListener("install", e => e.waitUntil(self.skipWaiting()));'
                'self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));',
                media_type="application/javascript", headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
        else:
            await application(scope, receive, send)
    return fixture


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
    with _native_fixture_runtime():
        from app.main import app
        uvicorn.run(_legacy_worker_fixture(app), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
