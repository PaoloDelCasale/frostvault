from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
import psycopg
from psycopg.rows import dict_row

from app import backoff
from app.catalog import ArchiveCatalog, VaultFileNotFound
from app.config import settings
from app.invites import (
    InviteError,
    create_invite,
    list_pending_invites,
    resolve_invite,
    revoke_invite,
)
from app.services import notifications
from app.services import user_administration
from app.system_settings import resolve_system_settings, set_system_setting


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "")
REPOSITORY_ROOT = Path(__file__).parents[1]


def run_alembic(
    revision: str = "head",
    command: str = "upgrade",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"database_url={POSTGRES_URL}",
            command,
            revision,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL is not configured")
class PostgreSQLMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sa.create_engine(POSTGRES_URL)
        with self.engine.begin() as connection:
            connection.execute(sa.text("DROP SCHEMA public CASCADE"))
            connection.execute(sa.text("CREATE SCHEMA public"))

    def tearDown(self) -> None:
        self.engine.dispose()

    def _connection(self) -> psycopg.Connection:
        return psycopg.connect(
            POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://"),
            row_factory=dict_row,
        )

    def _seed_rename_candidate(self) -> str:
        digest = "a" * 64
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )
            catalog = ArchiveCatalog(connection)
            missing_id = catalog.observe_local_copy(
                vault_id=2,
                path="concurrent/old.txt",
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="concurrent/old.txt",
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            catalog.mark_local_copy_missing(
                missing_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=2,
                path="concurrent/new.txt",
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="concurrent/new.txt",
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
        return missing_id

    def test_fresh_database_upgrades_and_rolls_back_losslessly(self) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        quota_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns("vault_quotas")
        }
        system_setting_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns("system_settings")
        }
        oidc_configuration_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns(
                "oidc_configuration"
            )
        }
        self.assertEqual(
            quota_columns,
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
        invite_columns = {
            column["name"] for column in sa.inspect(self.engine).get_columns("invites")
        }
        self.assertLessEqual({"revoked_at", "revoked_by"}, invite_columns)
        connect_url = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('settings-admin', 'Settings Admin', 'hash', TRUE)
                RETURNING id
                """
            ).fetchone()["id"]
            set_system_setting(
                connection,
                key="restore_tier",
                value="Standard",
                updated_by=admin_id,
            )
            resolved = resolve_system_settings(
                connection,
                settings_obj=settings,
                environ={},
            )
        self.assertEqual(resolved["restore_tier"].value, "Standard")
        self.assertEqual(
            resolved["restore_tier"].source,
            "database_override",
        )

        downgraded = run_alembic("0001_current_schema", command="downgrade")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)

        upgraded_again = run_alembic()
        self.assertEqual(upgraded_again.returncode, 0, upgraded_again.stderr)

    def test_current_release_data_migrates_without_loss(self) -> None:
        baseline = run_alembic("0001_current_schema")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (
                        2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote'
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (2, 7, 'owner')"
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO files(
                        vault_id, path, local_exists, local_size, local_mtime,
                        cloud_exists, cloud_size, cloud_key, storage_class, etag,
                        last_local_scan, last_cloud_scan, updated_at
                    ) VALUES (
                        2, 'report.txt', 1, 12, 1750000000.25,
                        1, 44, 'docs/report.txt', 'STANDARD', 'etag',
                        '2026-07-20', '2026-07-20', '2026-07-20'
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO jobs(
                        id, vault_id, path, action, status, requested_by,
                        requested_at, updated_at, total_bytes, transferred_bytes
                    ) VALUES (
                        9, 2, 'report.txt', 'upload', 'completed', 7,
                        '2026-07-20', '2026-07-20', 12, 12
                    )
                    """
                )
            )
            connection.execute(sa.text("DROP TABLE alembic_version"))

        migrated = run_alembic()
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.engine.connect() as connection:
            preserved = connection.execute(
                sa.text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM users WHERE id=7) AS users,
                        (SELECT COUNT(*) FROM vaults WHERE id=2) AS vaults,
                        (SELECT COUNT(*) FROM vault_files WHERE vault_id=2) AS files,
                        (SELECT COUNT(*) FROM jobs WHERE id=9) AS jobs,
                        (
                            SELECT COUNT(*) FROM archive_versions
                            WHERE origin='legacy' AND integrity='unverified'
                        ) AS legacy_versions
                    """
                )
            ).mappings().one()

        self.assertEqual(dict(preserved), {
            "users": 1,
            "vaults": 1,
            "files": 1,
            "jobs": 1,
            "legacy_versions": 1,
        })

    def test_vault_ownership_migration_preserves_normal_and_narrows_multi_owner_data(self) -> None:
        baseline = run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            for user_id in range(1, 5):
                connection.execute(
                    sa.text(
                        "INSERT INTO users(id, username, display_name, password_hash, "
                        "is_admin) VALUES (:id, :username, :display_name, 'hash', FALSE)"
                    ),
                    {
                        "id": user_id,
                        "username": f"user-{user_id}",
                        "display_name": f"User {user_id}",
                    },
                )
            for vault_id, slug in ((1, "normal"), (2, "multi")):
                connection.execute(
                    sa.text(
                        "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                        "s3_prefix, rclone_remote) VALUES (:id, :slug, :name, "
                        "'/source', 'bucket', :slug, 'remote')"
                    ),
                    {"id": vault_id, "slug": slug, "name": slug.title()},
                )
            connection.execute(
                sa.text(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner'), (1, 2, 'viewer'), "
                    "(2, 3, 'owner'), (2, 4, 'owner')"
                )
            )

        migrated = run_alembic("0007_vault_ownership")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.engine.connect() as connection:
            roles = connection.execute(
                sa.text(
                    "SELECT vault_id, user_id, role FROM vault_members "
                    "ORDER BY vault_id, user_id"
                )
            ).mappings().all()
        self.assertEqual(
            [(row["vault_id"], row["user_id"], row["role"]) for row in roles],
            [
                (1, 1, "owner"),
                (1, 2, "viewer"),
                (2, 3, "owner"),
                (2, 4, "operator"),
            ],
        )

    def test_zero_owner_vault_fails_atomically_without_widening_access(self) -> None:
        baseline = run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'viewer', 'Viewer', 'hash', FALSE)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                    "s3_prefix, rclone_remote) VALUES (1, 'orphan', 'Orphan', "
                    "'/source', 'bucket', 'orphan', 'remote')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'viewer')"
                )
            )

        migrated = run_alembic("0007_vault_ownership")
        self.assertNotEqual(migrated.returncode, 0)
        self.assertIn("vault 1 has no primary owner", migrated.stderr)
        self.assertIn("assign exactly one authorized existing member", migrated.stderr)
        with self.engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            role = connection.execute(
                sa.text(
                    "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=1"
                )
            ).scalar_one()
            columns = {
                column["name"]
                for column in sa.inspect(self.engine).get_columns("vaults")
            }
        self.assertEqual(version, "0006_auth_backoff")
        self.assertEqual(role, "viewer")
        self.assertNotIn("uuid", columns)

    def test_ownerless_vault_without_memberships_fails_atomically(self) -> None:
        baseline = run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                    "s3_prefix, rclone_remote) VALUES (1, 'orphan', 'Orphan', "
                    "'/source', 'bucket', 'orphan', 'remote')"
                )
            )

        migrated = run_alembic("0007_vault_ownership")
        self.assertNotEqual(migrated.returncode, 0)
        self.assertIn("vault 1 has no primary owner", migrated.stderr)
        with self.engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            columns = {
                column["name"]
                for column in sa.inspect(self.engine).get_columns("vaults")
            }
        self.assertEqual(version, "0006_auth_backoff")
        self.assertNotIn("uuid", columns)

    def test_duplicate_job_does_not_abort_the_catalog_transaction(self) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (
                        2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote'
                    )
                    """
                )
            )

        connect_url = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="report.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            arguments = {
                "vault_id": 2,
                "path": "report.txt",
                "action": "upload",
                "requested_by": 7,
                "requested_at": "2026-07-21T10:01:00+00:00",
                "group_id": "group",
                "is_directory": False,
            }
            first, _, _ = catalog.queue_jobs(**arguments)
            duplicate, _, _ = catalog.queue_jobs(**arguments)
            catalog.observe_local_copy(
                vault_id=2,
                path="after-duplicate.txt",
                file_type="regular",
                size=1,
                mtime_ns=2,
                observed_at="2026-07-21T10:02:00+00:00",
            )

        self.assertEqual(len(first), 1)
        self.assertEqual(duplicate, [])

    def test_terminal_job_commits_when_notification_savepoint_fails_on_postgresql(
        self,
    ) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

        connect_url = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('notification-owner', 'Notification Owner', 'hash', FALSE)
                RETURNING id
                """
            ).fetchone()["id"]
            vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('notification-vault', 'Notification Vault', '/source',
                          'bucket', 'notifications', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO vault_members(vault_id, user_id, role)
                VALUES (%s, %s, 'owner')
                """,
                (vault_id, user_id),
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=vault_id,
                path="terminal.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-22T10:00:00+00:00",
            )
            job_ids, _, _ = catalog.queue_jobs(
                vault_id=vault_id,
                path="terminal.txt",
                action="upload",
                requested_by=user_id,
                requested_at="2026-07-22T10:01:00+00:00",
                group_id="notification-regression",
                is_directory=False,
            )
            self.assertEqual(len(job_ids), 1)
            job_id = job_ids[0]
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s", (job_id,)
            )

            def fail_inside_savepoint(connection, *, job_id):
                connection.execute(
                    """
                    INSERT INTO notifications(
                        user_id, vault_id, job_id, event, title, body, created_at
                    ) VALUES (%s, %s, %s, 'job_completed', 'Partial', '', %s)
                    """,
                    (user_id, vault_id, job_id, "2026-07-22T10:02:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO missing_notification_table(id) VALUES (1)"
                )

            with patch.object(
                notifications,
                "enqueue_job_terminal_push",
                side_effect=fail_inside_savepoint,
            ):
                enqueued = notifications.enqueue_job_terminal_notification_best_effort(
                    connection, job_id=job_id
                )
            self.assertEqual(enqueued, 0)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()["status"],
                "completed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM notifications WHERE job_id=%s",
                    (job_id,),
                ).fetchone()["total"],
                0,
            )

        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()["status"],
                "completed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM notifications WHERE job_id=%s",
                    (job_id,),
                ).fetchone()["total"],
                0,
            )

    def test_invite_revocation_and_last_administrator_hold_on_postgresql(self) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

        connect_url = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('only-admin', 'Only Admin', 'hash', TRUE)
                RETURNING id
                """
            ).fetchone()["id"]
            member_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('member', 'Member', 'hash', FALSE)
                RETURNING id
                """
            ).fetchone()["id"]

            raw_token = create_invite(
                connection, target_user_id=member_id, created_by=admin_id
            )
            invite_id = list_pending_invites(connection)[0]["id"]
            revoke_invite(connection, invite_id=invite_id, actor_user_id=admin_id)

            self.assertEqual(list_pending_invites(connection), [])
            with self.assertRaises(InviteError) as revoked:
                resolve_invite(connection, raw_token=raw_token)
            self.assertEqual(revoked.exception.reason, "revoked")

            postgresql_settings = replace(settings, db_backend="postgresql")
            with patch(
                "app.services.user_administration.settings", postgresql_settings
            ):
                with self.assertRaises(
                    user_administration.AdministrationError
                ) as demoted:
                    user_administration.update_user(
                        connection,
                        user_id=admin_id,
                        actor_user_id=member_id,
                        is_admin=False,
                    )
            self.assertEqual(demoted.exception.reason, "last_admin")

            still_admin = connection.execute(
                "SELECT is_admin FROM users WHERE id=%s", (admin_id,)
            ).fetchone()
            self.assertTrue(still_admin["is_admin"])

    def test_issue_188_concurrent_confirms_consume_one_candidate_once(self) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        missing_id = self._seed_rename_candidate()
        start = threading.Barrier(2)

        def confirm_once(_: int) -> str:
            connection = self._connection()
            try:
                try:
                    with connection:
                        start.wait(timeout=20)
                        ArchiveCatalog(connection).confirm_file_rename(
                            vault_file_id=missing_id,
                            new_path="concurrent/new.txt",
                            changed_at="2026-07-21T11:05:00+00:00",
                            vault_id=2,
                        )
                except VaultFileNotFound:
                    return "not_found"
                return "confirmed"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(confirm_once, range(2)))

        self.assertEqual(outcomes.count("confirmed"), 1, outcomes)
        self.assertEqual(outcomes.count("not_found"), 1, outcomes)
        with self._connection() as connection:
            catalog = ArchiveCatalog(connection)
            current = catalog.get_file_by_path(2, "concurrent/new.txt")
            history = catalog.list_path_history(missing_id, vault_id=2)
            retired = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_files WHERE status='retired'"
            ).fetchone()["total"]
            copies = connection.execute(
                "SELECT COUNT(*) AS total FROM local_copies"
            ).fetchone()["total"]

        self.assertIsNotNone(current)
        self.assertEqual(current["id"], missing_id)
        self.assertEqual(
            [entry["path"] for entry in history],
            ["concurrent/old.txt", "concurrent/new.txt"],
        )
        self.assertEqual(retired, 1)
        self.assertEqual(copies, 1)

    def test_issue_188_scan_state_change_wins_before_confirmation_claim(self) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        missing_id = self._seed_rename_candidate()
        scanner_updated = threading.Event()
        release_scanner = threading.Event()
        scanner_done = threading.Event()
        scanner_errors: list[BaseException] = []

        def scanner_state_change() -> None:
            connection = self._connection()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE local_copies
                        SET presence='present', plaintext_sha256=%s, observed_at=%s
                        WHERE vault_file_id=%s
                        """,
                        ("b" * 64, "2026-07-21T12:00:00+00:00", missing_id),
                    )
                    scanner_updated.set()
                    if not release_scanner.wait(timeout=20):
                        raise TimeoutError("test did not release scanner transaction")
            except BaseException as exc:  # Preserve failures from the worker thread.
                scanner_errors.append(exc)
            finally:
                connection.close()
                scanner_done.set()

        scanner = threading.Thread(target=scanner_state_change)
        scanner.start()
        self.addCleanup(release_scanner.set)
        self.addCleanup(scanner.join)
        self.assertTrue(scanner_updated.wait(timeout=20))

        reservation_attempted = threading.Event()
        original_reservation = ArchiveCatalog._reserve_rename_confirmation

        def note_reservation(catalog: ArchiveCatalog, vault_id: int) -> None:
            reservation_attempted.set()
            original_reservation(catalog, vault_id)

        confirmation_outcome: list[str] = []

        def confirm_while_scanner_holds_copy_lock() -> None:
            connection = self._connection()
            try:
                try:
                    with connection:
                        ArchiveCatalog(connection).confirm_file_rename(
                            vault_file_id=missing_id,
                            new_path="concurrent/new.txt",
                            changed_at="2026-07-21T12:01:00+00:00",
                            vault_id=2,
                        )
                except VaultFileNotFound:
                    confirmation_outcome.append("not_found")
                else:
                    confirmation_outcome.append("confirmed")
            finally:
                connection.close()

        confirmer = threading.Thread(target=confirm_while_scanner_holds_copy_lock)
        try:
            with patch.object(
                ArchiveCatalog,
                "_reserve_rename_confirmation",
                autospec=True,
                side_effect=note_reservation,
            ):
                confirmer.start()
                self.assertTrue(reservation_attempted.wait(timeout=20))
                release_scanner.set()
                self.assertTrue(scanner_done.wait(timeout=20))
                confirmer.join(timeout=20)
        finally:
            release_scanner.set()
            scanner.join(timeout=20)
            confirmer.join(timeout=20)

        self.assertFalse(scanner.is_alive())
        self.assertFalse(confirmer.is_alive())
        self.assertEqual(scanner_errors, [])
        self.assertEqual(confirmation_outcome, ["not_found"])
        with self._connection() as connection:
            source_copy = connection.execute(
                """
                SELECT presence, plaintext_sha256
                FROM local_copies WHERE vault_file_id=%s
                """,
                (missing_id,),
            ).fetchone()
            active = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_files WHERE status='active'"
            ).fetchone()["total"]
            current_paths = connection.execute(
                "SELECT COUNT(*) AS total FROM file_paths WHERE valid_to IS NULL"
            ).fetchone()["total"]

        self.assertEqual(source_copy["presence"], "present")
        self.assertEqual(source_copy["plaintext_sha256"], "b" * 64)
        self.assertEqual(active, 2)
        self.assertEqual(current_paths, 2)

    def test_auth_backoff_concurrent_threshold_results_keep_matching_deadlines(
        self,
    ) -> None:
        """A real PostgreSQL row lock preserves each count/deadline pair."""
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        attempts = backoff.THRESHOLD + 2
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        start = threading.Barrier(attempts)

        def record() -> tuple[int, str | None]:
            with self._connection() as connection:
                start.wait(timeout=20)
                return backoff.record_failure(
                    connection,
                    scope="ip",
                    key="concurrent-backoff",
                )

        with patch.object(backoff, "_now", return_value=now):
            with ThreadPoolExecutor(max_workers=attempts) as pool:
                results = list(pool.map(lambda _: record(), range(attempts)))

        expected = [
            (
                count,
                (
                    now
                    + timedelta(seconds=backoff._backoff_seconds(count))
                ).isoformat()
                if backoff._backoff_seconds(count)
                else None,
            )
            for count in range(1, attempts + 1)
        ]
        self.assertEqual(sorted(results), expected)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT failure_count, next_allowed_at
                FROM auth_backoff
                WHERE scope='ip' AND key='concurrent-backoff'
                """
            ).fetchone()
        self.assertEqual(
            (row["failure_count"], row["next_allowed_at"]),
            expected[-1],
        )
