from __future__ import annotations

import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import (
    DatabaseSchemaError,
    SQLiteConnection,
    initialize_database,
)


def run_alembic(
    path: Path,
    revision: str = "head",
    command: str = "upgrade",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"database_url=sqlite:///{path.as_posix()}",
            command,
            revision,
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )


class DatabaseMigrationTests(unittest.TestCase):
    def test_startup_rejects_an_unmigrated_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "unmigrated.db")
            test_settings = SimpleNamespace(db_backend="sqlite", sqlite_path=path)

            with patch("app.database.settings", test_settings):
                with self.assertRaisesRegex(
                    DatabaseSchemaError,
                    "AUTO_MIGRATE|alembic upgrade head|backup_upgrade",
                ):
                    initialize_database()

            with SQLiteConnection(path) as connection:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            self.assertEqual(tables, [])

    def test_fresh_sqlite_database_migrates_to_a_startable_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)

            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(path),
                bootstrap_admin_username="admin",
                bootstrap_admin_password="a-secure-test-password",
                bootstrap_admin_display_name="Administrator",
                bootstrap_vault_slug="",
            )
            with patch("app.database.settings", test_settings):
                initialize_database()
            with SQLiteConnection(str(path)) as connection:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(vault_quotas)").fetchall()
                }
                system_setting_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(system_settings)"
                    ).fetchall()
                }
                oidc_configuration_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(oidc_configuration)"
                    ).fetchall()
                }
            self.assertEqual(
                columns,
                {
                    "vault_id",
                    "storage_soft_limit_bytes",
                    "storage_hard_limit_bytes",
                    "concurrency_soft_limit",
                    "concurrency_hard_limit",
                    "restore_30d_soft_limit_bytes",
                    "restore_30d_hard_limit_bytes",
                },
            )
            self.assertEqual(
                system_setting_columns,
                {"key", "value", "updated_by", "updated_at"},
            )
            self.assertEqual(
                oidc_configuration_columns,
                {
                    "id",
                    "active_enabled",
                    "active_version",
                    "active_issuer",
                    "active_client_id",
                    "active_secret_ciphertext",
                    "active_scopes",
                    "active_login_ttl_seconds",
                    "draft_version",
                    "draft_issuer",
                    "draft_client_id",
                    "draft_secret_ciphertext",
                    "draft_scopes",
                    "draft_login_ttl_seconds",
                    "validated_draft_version",
                    "validation_status",
                    "validation_error",
                    "validated_at",
                    "updated_by",
                    "updated_at",
                },
            )

    def test_current_catalog_migrates_without_losing_file_state(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.db"
            baseline = run_alembic(path, "0001_current_schema")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (
                        2, 'photos', 'Photos', '/source', 'bucket', 'vaults/photos',
                        'photos-crypt'
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (2, 1, 'owner')"
                )
                connection.execute(
                    """
                    INSERT INTO files(
                        vault_id, path, local_exists, local_size, local_mtime,
                        cloud_exists, cloud_size, cloud_key, storage_class, etag,
                        restore_state, restore_expiry, last_local_scan,
                        last_restore_scan, last_cloud_scan, updated_at
                    ) VALUES (
                        2, 'docs/report.txt', 1, 12, 1750000000.25,
                        1, 44, 'vaults/photos/docs/report.txt.bin',
                        'DEEP_ARCHIVE', 'legacy-etag', NULL, NULL,
                        '2026-07-20T10:00:00+00:00', NULL,
                        '2026-07-20T11:00:00+00:00',
                        '2026-07-20T11:00:00+00:00'
                    )
                    """
                )
                connection.execute("DROP TABLE alembic_version")

            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                file = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/report.txt"
                )

            self.assertEqual(file["local_copy"]["presence"], "present")
            self.assertEqual(file["local_copy"]["size"], 12)
            self.assertEqual(file["latest_version"]["size"], 44)
            self.assertEqual(file["latest_version"]["integrity"], "unverified")
            self.assertEqual(file["latest_version"]["availability"], "unknown")
            self.assertIsNone(file["latest_version"]["provider_version_id"])

    def test_adoption_rejects_schema_missing_a_required_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-current.db"
            baseline = run_alembic(path, "0001_current_schema")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute("DROP INDEX jobs_one_active_action_uq")
                connection.execute("DROP TABLE alembic_version")

            migrated = run_alembic(path)

            self.assertNotEqual(migrated.returncode, 0)
            self.assertIn("missing required index", migrated.stderr)

    def test_catalog_observes_a_new_local_file_through_one_interface(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                catalog = ArchiveCatalog(connection)
                file_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/annual.txt",
                    file_type="regular",
                    size=21,
                    mtime_ns=1_750_000_000_250_000_000,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                observed = catalog.get_file_by_path(2, "reports/annual.txt")

            self.assertEqual(observed["id"], file_id)
            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(observed["local_copy"]["mtime_ns"], 1_750_000_000_250_000_000)
            self.assertIsNone(observed["latest_version"])

    def test_confirmed_rename_preserves_identity_and_path_history(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rename.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'photos', 'Photos', '/source', 'bucket', 'photos', 'remote')
                    """
                )
                catalog = ArchiveCatalog(connection)
                file_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="test/fototest.jpg",
                    file_type="regular",
                    size=21,
                    mtime_ns=10,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.rename_file(
                    file_id,
                    new_path="test/fotodefinitiva.jpg",
                    changed_at="2026-07-21T11:00:00+00:00",
                )
                renamed = catalog.get_file_by_path(
                    2, "test/fotodefinitiva.jpg"
                )
                old_path = catalog.get_file_by_path(2, "test/fototest.jpg")
                history = catalog.list_path_history(file_id)

            self.assertEqual(renamed["id"], file_id)
            self.assertIsNone(old_path)
            self.assertEqual(
                [entry["path"] for entry in history],
                ["test/fototest.jpg", "test/fotodefinitiva.jpg"],
            )

    def test_stale_cloud_scan_does_not_hide_a_concurrent_upload(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent-upload.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                catalog = ArchiveCatalog(connection)
                catalog.record_archive_version(
                    vault_id=2,
                    path="old.txt",
                    object_key="docs/old.txt",
                    provider_version_id="old-version",
                    size=1,
                    storage_class="STANDARD",
                    etag="old",
                    uploaded_at="2026-07-21T09:00:00+00:00",
                    observed_at="2026-07-21T09:00:00+00:00",
                    scan_id="2026-07-21T09:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="new.txt",
                    object_key="docs/new.txt",
                    provider_version_id="new-version",
                    size=1,
                    storage_class="STANDARD",
                    etag="new",
                    uploaded_at="2026-07-21T11:00:00+00:00",
                    observed_at="2026-07-21T11:00:00+00:00",
                    scan_id="2026-07-21T11:00:00+00:00",
                    origin="upload",
                )
                catalog.mark_unseen_archive_versions_missing(
                    vault_id=2,
                    scan_id="2026-07-21T10:00:00+00:00",
                    scan_started_at="2026-07-21T10:00:00+00:00",
                )
                old = catalog.list_versions(2, "old.txt")
                new = catalog.list_versions(2, "new.txt")

            self.assertEqual(old[0]["availability"], "missing")
            self.assertEqual(new[0]["availability"], "available")

    def test_lossless_schema_can_downgrade_and_upgrade_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

            downgraded = run_alembic(
                path,
                "0001_current_schema",
                command="downgrade",
            )
            self.assertEqual(downgraded.returncode, 0, downgraded.stderr)

            upgraded_again = run_alembic(path)
            self.assertEqual(upgraded_again.returncode, 0, upgraded_again.stderr)

    def test_head_creates_oidc_login_and_user_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oidc.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

            with SQLiteConnection(str(path)) as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertIn("oidc_login", names)
            self.assertIn("user_identities", names)

            downgraded = run_alembic(
                path,
                "0003_server_side_sessions",
                command="downgrade",
            )
            self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertNotIn("oidc_login", names)
            self.assertNotIn("user_identities", names)
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protected.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                catalog = ArchiveCatalog(connection)
                for number in (1, 2):
                    catalog.record_archive_version(
                        vault_id=2,
                        path="report.txt",
                        object_key="docs/report.txt",
                        provider_version_id=f"version-{number}",
                        size=number,
                        storage_class="STANDARD",
                        etag=f"etag-{number}",
                        uploaded_at=f"2026-07-2{number}T10:00:00+00:00",
                        observed_at=f"2026-07-2{number}T10:00:00+00:00",
                        scan_id=f"2026-07-2{number}T10:00:00+00:00",
                    )

            downgraded = run_alembic(
                path,
                "0001_current_schema",
                command="downgrade",
            )

            self.assertNotEqual(downgraded.returncode, 0)
            self.assertIn("would lose versioned archive data", downgraded.stderr)

    def test_head_creates_invites_and_makes_password_nullable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invites.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

            with SQLiteConnection(str(path)) as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("invites", names)
                columns = {
                    row["name"]: row
                    for row in connection.execute(
                        "PRAGMA table_info(invites)"
                    ).fetchall()
                }
                self.assertEqual(
                    set(columns),
                    {
                        "id",
                        "token_hash",
                        "target_user_id",
                        "created_by",
                        "created_at",
                        "expires_at",
                        "redeemed_at",
                        "redeemed_issuer",
                        "redeemed_subject",
                    },
                )
                password = {
                    row["name"]: row
                    for row in connection.execute(
                        "PRAGMA table_info(users)"
                    ).fetchall()
                }["password_hash"]
                self.assertEqual(password["notnull"], 0)
                # The case-insensitive username guard survives the users recreate.
                connection.execute(
                    """
                    INSERT INTO users(username, display_name, password_hash, is_admin)
                    VALUES ('dana', 'Dana', 'hash', FALSE)
                    """
                )
                with self.assertRaises(Exception):
                    connection.execute(
                        """
                        INSERT INTO users(username, display_name, password_hash)
                        VALUES ('DANA', 'Dupe', 'hash')
                        """
                    )
            with SQLiteConnection(str(path)) as connection:
                # A shell user with no password can be inserted after this migration.
                connection.execute(
                    """
                    INSERT INTO users(username, display_name, password_hash, is_admin)
                    VALUES ('shell', 'Shell', NULL, FALSE)
                    """
                )
                # Passwordless Users block a downgrade that restores NOT NULL, so
                # drop it before exercising the schema rollback below.
                connection.execute("DELETE FROM users WHERE username='shell'")

            downgraded = run_alembic(
                path,
                "0004_oidc_login",
                command="downgrade",
            )
            self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertNotIn("invites", names)

    def test_invites_migration_preserves_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preserve.db"
            baseline = run_alembic(path, "0004_oidc_login")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                user_id = connection.execute(
                    """
                    INSERT INTO users(username, display_name, password_hash, is_admin)
                    VALUES ('carol', 'Carol', 'hash', TRUE)
                    RETURNING id
                    """
                ).fetchone()["id"]
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, user_id, token_hash, auth_method, csrf_token,
                        session_version, created_at, last_seen_at,
                        idle_expires_at, absolute_expires_at
                    ) VALUES (
                        'sess-1', %s, 'th', 'local', 'csrf', 1,
                        '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00',
                        '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00'
                    )
                    """,
                    (user_id,),
                )
                connection.execute(
                    """
                    INSERT INTO user_identities(user_id, issuer, subject, created_at)
                    VALUES (%s, 'https://issuer.example', 'subject-1',
                            '2026-07-21T00:00:00+00:00')
                    """,
                    (user_id,),
                )

            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

            with SQLiteConnection(str(path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) AS total FROM users"
                    ).fetchone()["total"],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) AS total FROM sessions"
                    ).fetchone()["total"],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) AS total FROM user_identities"
                    ).fetchone()["total"],
                    1,
                )

    def test_head_creates_auth_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backoff.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

            with SQLiteConnection(str(path)) as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("auth_backoff", names)
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(auth_backoff)"
                    ).fetchall()
                }
                self.assertEqual(
                    columns,
                    {
                        "id",
                        "scope",
                        "key",
                        "failure_count",
                        "next_allowed_at",
                        "updated_at",
                    },
                )
                # (scope, key) is unique so throttle counters are one per pair.
                connection.execute(
                    """
                    INSERT INTO auth_backoff(scope, key, failure_count, updated_at)
                    VALUES ('ip', '203.0.113.5', 1, '2026-07-21T00:00:00+00:00')
                    """
                )
                with self.assertRaises(Exception):
                    connection.execute(
                        """
                        INSERT INTO auth_backoff(scope, key, failure_count, updated_at)
                        VALUES ('ip', '203.0.113.5', 2, '2026-07-21T00:00:00+00:00')
                        """
                    )
            with SQLiteConnection(str(path)) as connection:
                # The scope is constrained to the two known throttle dimensions.
                with self.assertRaises(Exception):
                    connection.execute(
                        """
                        INSERT INTO auth_backoff(scope, key, failure_count, updated_at)
                        VALUES ('bogus', 'x', 1, '2026-07-21T00:00:00+00:00')
                        """
                    )

            downgraded = run_alembic(
                path,
                "0005_invites",
                command="downgrade",
            )
            self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertNotIn("auth_backoff", names)

    def test_downgrade_refuses_to_resurrect_a_missing_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-version.db"
            baseline = run_alembic(path, "0001_current_schema")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (2, 7, 'owner')"
                )
                connection.execute(
                    """
                    INSERT INTO files(
                        vault_id, path, local_exists, cloud_exists, cloud_key,
                        updated_at
                    ) VALUES (
                        2, 'missing.txt', 0, 1, 'docs/missing.txt', '2026-07-20'
                    )
                    """
                )

            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    UPDATE archive_versions
                    SET availability='missing',
                        availability_checked_at='2026-07-21T10:00:00+00:00'
                    """
                )

            downgraded = run_alembic(
                path,
                "0001_current_schema",
                command="downgrade",
            )

            self.assertNotEqual(downgraded.returncode, 0)
            self.assertIn("archive_availability", downgraded.stderr)

    def test_upgrade_blocks_conflicting_active_legacy_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conflicting-jobs.db"
            baseline = run_alembic(path, "0001_current_schema")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (2, 7, 'owner')"
                )
                connection.execute(
                    """
                    INSERT INTO files(
                        vault_id, path, local_exists, cloud_exists, updated_at
                    ) VALUES (2, 'report.txt', 1, 1, '2026-07-20')
                    """
                )
                for job_id, action in ((1, "upload"), (2, "recover")):
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            id, vault_id, path, action, status, requested_by,
                            requested_at, updated_at
                        ) VALUES (%s, 2, 'report.txt', %s, 'queued', 7,
                                  '2026-07-20', '2026-07-20')
                        """,
                        (job_id, action),
                    )
                connection.execute("DROP TABLE alembic_version")

            migrated = run_alembic(path)

            self.assertNotEqual(migrated.returncode, 0)
            self.assertIn(
                "multiple non-terminal jobs target one file",
                migrated.stderr,
            )
            with SQLiteConnection(str(path)) as connection:
                legacy_file = connection.execute(
                    "SELECT path FROM files WHERE vault_id=2"
                ).fetchone()
            self.assertEqual(legacy_file, {"path": "report.txt"})

    def test_cleanup_action_migration_preserves_existing_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.db"
            baseline = run_alembic(path, "0001_current_schema")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (2, 7, 'owner')"
                )
                connection.execute(
                    """
                    INSERT INTO files(
                        vault_id, path, local_exists, local_size, cloud_exists,
                        updated_at
                    ) VALUES (2, 'docs/file.txt', 1, 9, 0, '2026-07-20')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO jobs(
                        id, vault_id, path, action, status, requested_by,
                        requested_at, updated_at, total_bytes, transferred_bytes
                    ) VALUES (
                        1, 2, 'docs/file.txt', 'upload', 'completed', 7,
                        '2026-07-20', '2026-07-20', 9, 9
                    )
                    """
                )
                connection.execute("DROP TABLE alembic_version")

            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                preserved = connection.execute(
                    """
                    SELECT j.action, j.total_bytes, fp.path
                    FROM jobs j
                    JOIN vault_files vf ON vf.id=j.vault_file_id
                    JOIN file_paths fp
                      ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                    WHERE j.id=1
                    """
                ).fetchone()

            self.assertEqual(
                preserved,
                {
                    "action": "upload",
                    "total_bytes": 9,
                    "path": "docs/file.txt",
                },
            )


if __name__ == "__main__":
    unittest.main()
