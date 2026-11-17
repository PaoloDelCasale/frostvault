"""Baseline schema and the schema guard (ADR-0035, ADR-0041, #601, #679).

A database is initialized only when empty, upgraded only from an earlier
revision of the linear chain, used at the head, and otherwise refused before
any write: at app start and in metadata restore.
Refusal tests make the database read-only first, so any write the guard
attempted would fail differently from the expected refusal.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app import database, schema_guard
from app.database import HEAD_SCHEMA_REVISION, DatabaseSchemaError, initialize_database
from app.schema_guard import SchemaState, inspect_schema, prepare_schema, require_current_schema
from tests.postgres import PostgresConnection, _server_dsn, database_dsn, database_state, run_alembic

# Kept for the many modules that import the fixture from here.
__all__ = ["run_alembic"]

BASELINE_TABLES = {
    "alembic_version", "owner", "vault_targets", "vaults", "files",
    "file_path_history", "archive_versions", "directory_aggregate_status",
    "directory_aggregate_dirty", "job_groups", "jobs", "job_leases", "job_uploads",
    "job_restores", "job_storage_class_changes", "restore_requests",
    "cloud_purge_requests", "scheduler_vault_turns", "job_terminal_notifications",
    "sessions", "oidc_login", "auth_backoff",
    "audit_events", "notifications", "notification_deliveries", "upload_snapshots",
    "upload_attempts", "upload_receipts", "upload_multiparts",
    "vault_operation_policies", "bulk_upload_admissions", "bulk_upload_exclusions",
    "cleanup_batches", "cleanup_candidates",
    "upload_remediations", "upload_remediation_jobs", "upload_remediation_sweep",
    "copy_attempts", "copy_receipts", "copy_multiparts", "source_volumes",
    "directory_aggregates", "vault_catalog_revisions", "catalog_events",
    "vault_component_health", "cloud_preflight_state",
    "metadata_backup_runs", "recovery_kit_custody", "metadata_restore",
    "metadata_restore_vaults", "metadata_restore_updates",
}
# Tables added by later linear revisions of the chain.
HEAD_TABLES = BASELINE_TABLES | {"vault_deletions", "system_bucket", "vault_bucket_provisioning"}


def _tables(path: Path) -> set[str]:
    with PostgresConnection(str(path)) as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {row["tablename"] for row in rows}


def _migrate_to(dsn: str, revision: str) -> None:
    """Bring an empty fixture database to ``revision`` exactly, like an old release."""
    from alembic import command
    from alembic.config import Config

    engine = schema_guard._engine(dsn)
    try:
        with engine.begin() as connection:
            config = Config(str(schema_guard._REPOSITORY_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(schema_guard._REPOSITORY_ROOT / "migrations"))
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def _revision(path: Path) -> list[str]:
    with PostgresConnection(str(path)) as connection:
        return [row["version_num"] for row in
                connection.execute("SELECT version_num FROM alembic_version").fetchall()]


def _make_read_only(path: Path) -> None:
    name = conninfo_to_dict(database_dsn(path))["dbname"]
    with psycopg.connect(_server_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = on").format(
                sql.Identifier(name)
            )
        )


class _GuardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            database_dsn="",
            operation_concurrency=1,
            bootstrap_admin_username="Owner",
            bootstrap_admin_password="correct horse battery staple",
            bootstrap_admin_display_name="The Owner",
        )
        patcher = patch.object(database, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(database.close_pools)
        self.fresh_database()

    def fresh_database(self) -> None:
        """Point the app at a new, empty fixture database."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "guard.db"
        self.dsn = self.settings.database_dsn = database_dsn(self.path)

    def execute(self, *statements: str) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for statement in statements:
                connection.execute(statement)

    def seed_old_catalog(self, revision: str = "0072_upload_lineage_indexes") -> None:
        self.execute(
            "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
            f"INSERT INTO alembic_version VALUES ('{revision}')",
            "CREATE TABLE users (id bigint PRIMARY KEY, username text NOT NULL)",
            "INSERT INTO users VALUES (1, 'owner')",
        )

    def assert_refused_without_writes(self, action, *fragments: str) -> None:
        _make_read_only(self.path)
        before = database_state(self.path)
        with self.assertRaises(DatabaseSchemaError) as raised:
            action()
        for fragment in fragments:
            self.assertIn(fragment, str(raised.exception))
        self.assertEqual(database_state(self.path), before)


class StartupGuardTests(_GuardFixture):
    def test_empty_database_is_initialized_at_the_baseline_with_one_owner(self) -> None:
        initialize_database()

        self.assertEqual(_tables(self.path), HEAD_TABLES)
        with PostgresConnection(str(self.path)) as connection:
            revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            owner = connection.execute("SELECT * FROM owner").fetchall()
        self.assertEqual(revisions, [{"version_num": HEAD_SCHEMA_REVISION}])
        self.assertEqual(len(owner), 1)
        self.assertEqual(owner[0]["display_name"], "The Owner")
        self.assertEqual(owner[0]["session_version"], 1)
        self.assertIsNone(owner[0]["oidc_subject"])

    def test_database_at_head_starts_without_any_write(self) -> None:
        initialize_database()
        database.close_pools()
        _make_read_only(self.path)
        before = database_state(self.path)

        initialize_database()

        self.assertEqual(database_state(self.path), before)
        with PostgresConnection(str(self.path)) as connection:
            self.assertIs(inspect_schema(connection), SchemaState.CURRENT)

    def test_old_revision_is_refused_before_any_write(self) -> None:
        self.seed_old_catalog()
        self.assert_refused_without_writes(
            initialize_database, "0072_upload_lineage_indexes", HEAD_SCHEMA_REVISION,
        )
        self.assertNotIn("owner", _tables(self.path))

    def test_catalog_of_the_frozen_baseline_is_upgraded_at_start(self) -> None:
        _migrate_to(self.dsn, "0001_baseline")
        self.execute(
            "INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Kept', 'x')"
        )
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        with PostgresConnection(str(self.path)) as connection:
            self.assertIs(inspect_schema(connection), SchemaState.CURRENT)
            columns = {row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'upload_snapshots'"
            ).fetchall()}
            owner = connection.execute("SELECT display_name FROM owner").fetchone()
        self.assertLessEqual({"owner_uid", "owner_gid", "file_mode"}, columns)
        self.assertEqual(owner["display_name"], "Kept")

    def _assert_local_part_digests(self) -> None:
        """#703: nullable part digests on files, cleared by a digest change."""
        with PostgresConnection(str(self.path)) as connection:
            columns = {row["column_name"]: row for row in connection.execute(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'files' AND column_name LIKE 'local_part_%'"
            ).fetchall()}
            trigger = connection.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = 'files_local_parts_follow_digest' "
                "AND NOT tgisinternal"
            ).fetchone()
        self.assertEqual(set(columns), {"local_part_size", "local_part_sha256"})
        for column in columns.values():
            self.assertEqual((column["is_nullable"], column["column_default"]), ("YES", None))
        self.assertIsNotNone(trigger)

    def test_empty_database_gets_the_local_part_digests(self) -> None:
        initialize_database()
        self._assert_local_part_digests()

    def test_catalog_of_the_previous_revision_is_upgraded_to_part_digests(self) -> None:
        _migrate_to(self.dsn, "0003_job_admitted_generation")
        self.execute(
            "INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Kept', 'x')"
        )
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        self._assert_local_part_digests()
        with PostgresConnection(str(self.path)) as connection:
            self.assertIs(inspect_schema(connection), SchemaState.CURRENT)
            owner = connection.execute("SELECT display_name FROM owner").fetchone()
        self.assertEqual(owner["display_name"], "Kept")

    def test_catalog_of_the_previous_revision_gets_duplicate_cleanup_off(self) -> None:
        """#726: every Vault starts with Automatic Duplicate Cleanup off, never run,
        and every existing deletion request stays the Owner's (not automatic)."""
        _migrate_to(self.dsn, "0005_vault_deletions")
        self.execute(
            "INSERT INTO vault_targets(id, provider, bucket, region) VALUES (1, 's3', 'b', 'eu-south-1')")
        self.execute(
            "INSERT INTO vaults(id, target_id, name, source_root) VALUES (1, 1, 'Docs', '/sources/docs')")
        self.execute(
            "INSERT INTO job_groups(id, vault_id, path, created_at) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 1, '', now())")
        self.execute(
            "INSERT INTO cloud_purge_requests(group_id, reason) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'manual')")
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        with PostgresConnection(str(self.path)) as connection:
            vault = connection.execute(
                """SELECT duplicate_cleanup_enabled, duplicate_cleanup_grace_days,
                          duplicate_cleanup_cap_files, duplicate_cleanup_cap_bytes,
                          duplicate_cleanup_last_run_at, duplicate_cleanup_last_outcome
                   FROM vaults WHERE id=1""").fetchone()
            request = connection.execute(
                "SELECT automatic FROM cloud_purge_requests").fetchone()
            for bad in ("duplicate_cleanup_grace_days=0", "duplicate_cleanup_grace_days=91",
                        "duplicate_cleanup_cap_files=0", "duplicate_cleanup_cap_bytes=0"):
                with self.subTest(bad=bad), self.assertRaises(psycopg.errors.CheckViolation):
                    connection.execute(f"UPDATE vaults SET {bad} WHERE id=1")
                connection.rollback()
            with self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(
                    "UPDATE cloud_purge_requests SET automatic=true, stop_protection=true")
            connection.rollback()
        self.assertEqual(dict(vault), {
            "duplicate_cleanup_enabled": False, "duplicate_cleanup_grace_days": 7,
            "duplicate_cleanup_cap_files": 5000, "duplicate_cleanup_cap_bytes": 50 * 1024 ** 3,
            "duplicate_cleanup_last_run_at": None, "duplicate_cleanup_last_outcome": None})
        self.assertFalse(request["automatic"])

    def test_catalog_of_the_previous_revision_gets_no_invented_cleanup_totals(self) -> None:
        """#729: the cleanup totals start NULL (never recorded), even when a
        completed automatic deletion is still listed: no backfill."""
        _migrate_to(self.dsn, "0006_duplicate_cleanup")
        group = "00000000-0000-0000-0000-000000000002"
        file_id = "00000000-0000-0000-0000-0000000000f1"
        self.execute(
            "INSERT INTO vault_targets(id, provider, bucket, region) VALUES (1, 's3', 'b', 'eu-south-1')")
        self.execute(
            "INSERT INTO vaults(id, target_id, name, source_root, duplicate_cleanup_enabled) "
            "VALUES (1, 1, 'Docs', '/sources/docs', true)")
        self.execute(
            f"INSERT INTO files(id, vault_id, path, path_since) VALUES ('{file_id}', 1, 'old/a.jpg', now())")
        self.execute(
            f"INSERT INTO job_groups(id, vault_id, path, created_at) VALUES ('{group}', 1, '', now())")
        self.execute(
            f"INSERT INTO cloud_purge_requests(group_id, reason, automatic) VALUES ('{group}', 'auto', true)")
        self.execute(
            "INSERT INTO jobs(vault_id, file_id, group_id, action, status, origin, total_bytes, finished_at) "
            f"VALUES (1, '{file_id}', '{group}', 'cloud-purge', 'completed', 'automatic', 10, now())")
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        with PostgresConnection(str(self.path)) as connection:
            vault = connection.execute(
                """SELECT duplicate_cleanup_enabled, duplicate_cleanup_deleted_count,
                          duplicate_cleanup_deleted_bytes, duplicate_cleanup_last_deleted_at
                   FROM vaults WHERE id=1""").fetchone()
            columns = {row["column_name"]: row for row in connection.execute(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'vaults' AND column_name IN ('duplicate_cleanup_deleted_count', "
                "'duplicate_cleanup_deleted_bytes', 'duplicate_cleanup_last_deleted_at')").fetchall()}
            for bad in ("duplicate_cleanup_deleted_count=1",
                        "duplicate_cleanup_deleted_count=-1, duplicate_cleanup_deleted_bytes=0, "
                        "duplicate_cleanup_last_deleted_at=now()",
                        "duplicate_cleanup_deleted_count=0, duplicate_cleanup_deleted_bytes=-1, "
                        "duplicate_cleanup_last_deleted_at=now()"):
                with self.subTest(bad=bad), self.assertRaises(psycopg.errors.CheckViolation):
                    connection.execute(f"UPDATE vaults SET {bad} WHERE id=1")
                connection.rollback()
        self.assertEqual(dict(vault), {
            "duplicate_cleanup_enabled": True, "duplicate_cleanup_deleted_count": None,
            "duplicate_cleanup_deleted_bytes": None, "duplicate_cleanup_last_deleted_at": None})
        self.assertEqual(len(columns), 3)
        for column in columns.values():
            self.assertEqual((column["is_nullable"], column["column_default"]), ("YES", None))

    def test_catalog_of_the_previous_revision_gets_no_invented_system_bucket(self) -> None:
        """#734: an upgrade creates no System Bucket row, and earlier off-host
        runs keep s3_bucket NULL: their shared bucket was never recorded."""
        _migrate_to(self.dsn, "0007_duplicate_cleanup_totals")
        self.execute(
            "INSERT INTO metadata_backup_runs(created_at, reason, backend, status, s3_key) "
            "VALUES (now(), 'scheduled', 'postgresql', 'succeeded', 'system/backups/a.bak.enc')")
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        with PostgresConnection(str(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM system_bucket").fetchone()["n"], 0)
            run = connection.execute("SELECT s3_key, s3_bucket FROM metadata_backup_runs").fetchone()
            self.assertEqual(dict(run), {"s3_key": "system/backups/a.bak.enc", "s3_bucket": None})
            with self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(
                    "INSERT INTO metadata_backup_runs(created_at, reason, backend, status, s3_bucket) "
                    "VALUES (now(), 'manual', 'postgresql', 'succeeded', 'frostvault-system-abcd1234')")

    def test_catalog_of_the_previous_revision_gets_no_invented_class_entry(self) -> None:
        """#731: every existing version keeps a NULL Class Entry (never
        recorded, estimated at read time) and the cleanup's minimum option is off."""
        _migrate_to(self.dsn, "0008_system_bucket")
        file_id = "00000000-0000-0000-0000-0000000000f2"
        self.execute(
            "INSERT INTO vault_targets(id, provider, bucket, region) VALUES (1, 's3', 'b', 'eu-south-1')")
        self.execute(
            "INSERT INTO vaults(id, target_id, name, source_root, duplicate_cleanup_enabled) "
            "VALUES (1, 1, 'Docs', '/sources/docs', true)")
        self.execute(
            f"INSERT INTO files(id, vault_id, path, path_since) VALUES ('{file_id}', 1, 'a.jpg', now())")
        self.execute(
            "INSERT INTO archive_versions(file_id, object_key, provider_version_id, size, storage_class, created_at) "
            f"VALUES ('{file_id}', 'vaults/x/a.jpg', 'v1', 10, 'DEEP_ARCHIVE', now() - interval '200 days')")
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        with PostgresConnection(str(self.path)) as connection:
            version = connection.execute(
                "SELECT storage_class, storage_class_since FROM archive_versions").fetchone()
            vault = connection.execute(
                "SELECT duplicate_cleanup_enabled, duplicate_cleanup_beyond_minimum FROM vaults WHERE id=1").fetchone()
            columns = {(row["table_name"], row["column_name"]): row for row in connection.execute(
                "SELECT table_name, column_name, is_nullable, column_default, data_type "
                "FROM information_schema.columns WHERE (table_name, column_name) IN "
                "(('archive_versions', 'storage_class_since'), ('vaults', 'duplicate_cleanup_beyond_minimum'))"
            ).fetchall()}
        self.assertEqual(dict(version), {"storage_class": "DEEP_ARCHIVE", "storage_class_since": None})
        self.assertEqual(dict(vault), {"duplicate_cleanup_enabled": True, "duplicate_cleanup_beyond_minimum": False})
        since = columns["archive_versions", "storage_class_since"]
        self.assertEqual((since["is_nullable"], since["column_default"], since["data_type"]),
                         ("YES", None, "timestamp with time zone"))
        option = columns["vaults", "duplicate_cleanup_beyond_minimum"]
        self.assertEqual((option["is_nullable"], option["column_default"]), ("NO", "false"))

    def test_catalog_of_the_previous_revision_keeps_its_shared_bucket_layout(self) -> None:
        """#735: an existing Vault keeps its shared bucket and vaults/<uuid>/
        prefix; nothing is invented (no Retention Lock, no provisioning)."""
        _migrate_to(self.dsn, "0009_class_entry")
        self.execute(
            "INSERT INTO vault_targets(id, provider, bucket, region) VALUES (1, 's3', 'shared', 'eu-south-1')",
            "INSERT INTO vaults(id, target_id, name, source_root) VALUES (1, 1, 'Docs', '/sources/docs')")
        self.assertIs(prepare_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        with PostgresConnection(str(self.path)) as connection:
            target = connection.execute(
                "SELECT bucket, key_layout, retention_lock_days FROM vault_targets").fetchone()
            vault = connection.execute("SELECT lifecycle FROM vaults").fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM vault_bucket_provisioning").fetchone()
        self.assertEqual(dict(target), {"bucket": "shared", "key_layout": "vault_prefix",
                                        "retention_lock_days": None})
        self.assertEqual((vault["lifecycle"], pending["n"]), ("active", 0))

    def test_failed_upgrade_leaves_the_catalog_at_its_revision(self) -> None:
        _migrate_to(self.dsn, "0001_baseline")
        upgrade = schema_guard._alembic_upgrade

        def upgrade_then_fail(connection) -> None:
            upgrade(connection)
            raise RuntimeError("simulated crash after DDL")

        with patch.object(schema_guard, "_alembic_upgrade", upgrade_then_fail):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                prepare_schema(self.dsn)
        self.assertEqual(_revision(self.path), ["0001_baseline"])

    def test_upgrade_schema_upgrades_but_never_initializes(self) -> None:
        from app.schema_guard import upgrade_schema

        self.assert_refused_without_writes(lambda: upgrade_schema(self.dsn), "empty")
        self.assertEqual(_tables(self.path), set())
        self.fresh_database()
        _migrate_to(self.dsn, "0001_baseline")
        self.assertIs(upgrade_schema(self.dsn), SchemaState.OUTDATED)
        self.assertEqual(_revision(self.path), [HEAD_SCHEMA_REVISION])
        self.assertIs(upgrade_schema(self.dsn), SchemaState.CURRENT)

    def test_outdated_catalog_is_refused_by_restore_registration(self) -> None:
        from app.services.recovery_kit import record_metadata_restore

        _migrate_to(self.dsn, "0001_baseline")

        def restore() -> None:
            with PostgresConnection(str(self.path)) as connection:
                record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")

        self.assert_refused_without_writes(restore, "earlier revision", "upgrade_schema")

    def test_nonempty_database_without_alembic_version_is_refused(self) -> None:
        objects = {
            "table": "CREATE TABLE users (id bigint)",
            "view": "CREATE VIEW leftover AS SELECT 1 AS one",
            "sequence": "CREATE SEQUENCE leftover_seq",
            "enum": "CREATE TYPE leftover_state AS ENUM ('a')",
            "function": "CREATE FUNCTION leftover() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "other schema": "CREATE SCHEMA legacy; CREATE TABLE legacy.vaults (id bigint)",
        }
        for kind, statement in objects.items():
            with self.subTest(kind=kind):
                self.fresh_database()
                self.execute(statement)
                self.assert_refused_without_writes(
                    initialize_database, "not empty", "no alembic_version",
                )
                with PostgresConnection(str(self.path)) as connection:
                    self.assertIsNone(connection.execute(
                        "SELECT to_regclass('alembic_version') AS present"
                    ).fetchone()["present"])

    def test_divergent_or_empty_revision_records_are_refused(self) -> None:
        cases = {
            "two revisions": [HEAD_SCHEMA_REVISION, "0072_upload_lineage_indexes"],
            "two chain revisions": ["0001_baseline", HEAD_SCHEMA_REVISION],
            "a newer release": ["0099_from_the_future"],
            "no revision row": [],
        }
        for case, revisions in cases.items():
            with self.subTest(case=case):
                self.fresh_database()
                self.execute(
                    "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
                    *(f"INSERT INTO alembic_version VALUES ('{r}')" for r in revisions),
                )
                self.assert_refused_without_writes(initialize_database, HEAD_SCHEMA_REVISION)

    def test_concurrent_starts_initialize_an_empty_database_once(self) -> None:
        barrier = threading.Barrier(3)
        results: list[object] = []

        def start() -> None:
            barrier.wait()
            try:
                results.append(prepare_schema(self.dsn))
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion
                results.append(exc)

        threads = [threading.Thread(target=start) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        outcomes = sorted(r.value if isinstance(r, SchemaState) else repr(r) for r in results)
        self.assertEqual(outcomes, ["current", "current", "empty"])
        self.assertEqual(_tables(self.path), HEAD_TABLES)

    def test_failed_initialization_leaves_the_database_empty(self) -> None:
        upgrade = schema_guard._alembic_upgrade

        def upgrade_then_fail(connection) -> None:
            upgrade(connection)
            raise RuntimeError("simulated crash after DDL")

        with patch.object(schema_guard, "_alembic_upgrade", upgrade_then_fail):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                prepare_schema(self.dsn)
        with PostgresConnection(str(self.path)) as connection:
            self.assertIs(inspect_schema(connection), SchemaState.EMPTY)

    def test_changed_emergency_password_revokes_sessions(self) -> None:
        initialize_database()
        self.settings.bootstrap_admin_password = "another long emergency password"
        initialize_database()
        with PostgresConnection(str(self.path)) as connection:
            owner = connection.execute("SELECT session_version FROM owner").fetchone()
        self.assertEqual(owner["session_version"], 2)


class RestoreGuardTests(_GuardFixture):
    def test_metadata_restore_refuses_an_old_catalog_before_any_write(self) -> None:
        from app.services.recovery_kit import record_metadata_restore

        self.seed_old_catalog()

        def restore() -> None:
            with PostgresConnection(str(self.path)) as connection:
                record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")

        self.assert_refused_without_writes(
            restore, "0072_upload_lineage_indexes", HEAD_SCHEMA_REVISION,
        )

    def test_metadata_restore_never_initializes_an_empty_database(self) -> None:
        from app.services.recovery_kit import record_metadata_restore

        def restore() -> None:
            with PostgresConnection(str(self.path)) as connection:
                record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")

        self.assert_refused_without_writes(restore, "empty")
        self.assertEqual(_tables(self.path), set())

    def test_metadata_restore_accepts_a_catalog_at_head(self) -> None:
        self.assertEqual(run_alembic(self.path).returncode, 0)
        with PostgresConnection(str(self.path)) as connection:
            require_current_schema(connection)

    def test_backup_verification_refuses_a_dump_of_another_schema(self) -> None:
        from app.services import metadata_backups

        self.seed_old_catalog()
        dump = metadata_backups._dump_postgres_bytes(self.dsn)
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(metadata_backups.BackupError, "0072_upload_lineage_indexes"):
                metadata_backups._verify_postgres_dump_isolated(dump, Path(work))

    def test_backup_verification_accepts_a_dump_of_an_earlier_chain_revision(self) -> None:
        from app.services import metadata_backups

        _migrate_to(self.dsn, "0001_baseline")
        self.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Kept', 'x')")
        dump = metadata_backups._dump_postgres_bytes(self.dsn)
        with tempfile.TemporaryDirectory() as work, patch.object(
            metadata_backups, "settings", SimpleNamespace(database_dsn=self.dsn),
        ):
            result = metadata_backups._verify_postgres_dump_isolated(dump, Path(work))
        self.assertEqual(result["schema_revision"], "0001_baseline")
        self.assertEqual(result["owner_count"], 1)

    def test_backup_verification_accepts_a_dump_at_head(self) -> None:
        from app.services import metadata_backups

        initialize_database()
        dump = metadata_backups._dump_postgres_bytes(self.dsn)
        with tempfile.TemporaryDirectory() as work, patch.object(
            metadata_backups, "settings", SimpleNamespace(database_dsn=self.dsn),
        ):
            result = metadata_backups._verify_postgres_dump_isolated(dump, Path(work))
        self.assertEqual(result["schema_revision"], HEAD_SCHEMA_REVISION)
        self.assertEqual(result["owner_count"], 1)


class BaselineRevisionTests(unittest.TestCase):
    def test_revisions_form_the_linear_chain_of_the_guard(self) -> None:
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        self.assertEqual(sorted(p.name for p in versions.glob("*.py")),
                         sorted([f"{r}.py" for r in schema_guard.SCHEMA_REVISIONS] + ["__init__.py"]))
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(versions.parents[1] / "alembic.ini"))
        config.set_main_option("script_location", str(versions.parent))
        scripts = ScriptDirectory.from_config(config)
        self.assertEqual(scripts.get_heads(), [HEAD_SCHEMA_REVISION])
        self.assertEqual(schema_guard.SCHEMA_REVISIONS[0], "0001_baseline")
        previous = None
        for revision in schema_guard.SCHEMA_REVISIONS:
            self.assertLessEqual(len(revision), 32)  # alembic_version.version_num
            self.assertEqual(scripts.get_revision(revision).down_revision, previous)
            previous = revision
        self.assertEqual(previous, HEAD_SCHEMA_REVISION)

    def test_migration_sql_has_no_accidental_sqlalchemy_bind_parameters(self) -> None:
        import importlib

        import sqlalchemy as sa

        for revision in schema_guard.SCHEMA_REVISIONS:
            with self.subTest(revision=revision):
                migration = importlib.import_module(f"migrations.versions.{revision}")
                with patch.object(migration, "op") as operations:
                    migration.upgrade()
                for call in operations.execute.call_args_list:
                    self.assertEqual(sa.text(call.args[0]).compile().params, {})


class BaselineSchemaTests(unittest.TestCase):
    """The core tables and the invariants their constraints enforce."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "baseline.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = PostgresConnection(str(self.path))
        self.addCleanup(self.connection.close)
        self.connection.autocommit = True

    def execute(self, statement: str, params: tuple = ()) -> list[dict]:
        cursor = self.connection.execute(statement, params)
        return cursor.fetchall() if cursor.description else []

    def rejects(self, statement: str, params: tuple = (), error=psycopg.errors.IntegrityError) -> None:
        with self.assertRaises(error):
            self.execute(statement, params)

    def target_id(self) -> int:
        existing = self.execute("SELECT id FROM vault_targets WHERE bucket = 'bucket'")
        return (existing or self.execute(
            "INSERT INTO vault_targets(provider, bucket, region, key_layout) "
            "VALUES ('s3', 'bucket', 'eu-south-1', 'vault_prefix') RETURNING id"
        ))[0]["id"]

    def vault(self, root: str = "/sources/managed/a", **values) -> dict:
        columns = {"name": "Docs", "source_root": root, "target_id": self.target_id(), **values}
        names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        return self.execute(
            f"INSERT INTO vaults({names}) VALUES ({placeholders}) RETURNING *",
            tuple(columns.values()),
        )[0]

    def test_no_user_tables_and_no_foreign_key_to_users(self) -> None:
        tables = {row["tablename"] for row in self.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )}
        self.assertEqual(tables, HEAD_TABLES)
        referenced = {row["referenced"] for row in self.execute(
            "SELECT confrelid::regclass::text AS referenced FROM pg_constraint WHERE contype = 'f'"
        )}
        self.assertEqual(referenced, {
            "vault_targets", "vaults", "files", "archive_versions", "job_groups", "jobs",
            "notifications", "upload_snapshots", "upload_attempts", "bulk_upload_admissions",
            "cleanup_batches", "upload_remediations", "copy_attempts",
        })
        # No per-user attribution survives anywhere (#586).
        attribution = {
            (row["table_name"], row["column_name"]) for row in self.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name = ANY(%s)",
                (["user_id", "actor_user_id", "requested_by", "approved_by", "username"],),
            )
        }
        self.assertEqual(attribution, set())

    def test_native_column_types(self) -> None:
        types = {
            (row["table_name"], row["column_name"]): row["data_type"]
            for row in self.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
        }
        expected = {
            ("vaults", "uuid"): "uuid",
            ("vaults", "created_at"): "timestamp with time zone",
            ("files", "id"): "uuid",
            ("files", "local_mtime_ns"): "bigint",
            ("files", "local_sha256"): "bytea",
            ("archive_versions", "plaintext_sha256"): "bytea",
            ("jobs", "group_id"): "uuid",
            ("jobs", "requested_at"): "timestamp with time zone",
            ("sessions", "id"): "uuid",
            ("sessions", "token_hash"): "bytea",
            ("sessions", "absolute_expires_at"): "timestamp with time zone",
            ("auth_backoff", "next_allowed_at"): "timestamp with time zone",
            ("audit_events", "detail"): "jsonb",
            ("notifications", "message_params"): "jsonb",
            ("notifications", "read_at"): "timestamp with time zone",
        }
        for column, data_type in expected.items():
            self.assertEqual(types[column], data_type, column)
        collations = {
            (row["table_name"], row["column_name"]) for row in self.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND collation_name = 'C'"
            )
        }
        for key in (("files", "path"), ("vaults", "source_root"),
                    ("archive_versions", "object_key"), ("archive_versions", "provider_version_id"),
                    ("owner", "oidc_subject"), ("vault_targets", "bucket")):
            self.assertIn(key, collations)

    def test_owner_is_a_single_row(self) -> None:
        self.execute("INSERT INTO owner(display_name, password_hash) VALUES ('Owner', 'hash')")
        self.rejects("INSERT INTO owner(display_name) VALUES ('Second')")
        self.rejects("INSERT INTO owner(id, display_name) VALUES (2, 'Second')")
        self.rejects("UPDATE owner SET id = 2")
        self.rejects("UPDATE owner SET password_hash = ''", error=psycopg.errors.CheckViolation)
        # The OIDC Identity is bound all at once, never half.
        self.rejects("UPDATE owner SET oidc_subject = 'subject'")
        self.execute(
            "UPDATE owner SET oidc_issuer = 'https://id.example', oidc_subject = 'subject', "
            "oidc_bound_at = now()"
        )

    def test_sessions_hold_only_a_token_digest(self) -> None:
        self.rejects(
            "INSERT INTO sessions(token_hash, csrf_token, auth_method, session_version, "
            "idle_expires_at, absolute_expires_at) "
            "VALUES ('raw-token', 'csrf', 'local', 1, now(), now())"
        )
        self.rejects(
            "INSERT INTO sessions(token_hash, csrf_token, auth_method, session_version, "
            "idle_expires_at, absolute_expires_at) "
            "VALUES (sha256('t'), 'csrf', 'password', 1, now(), now())"
        )

    def test_notifications_are_deduplicated_without_a_user(self) -> None:
        self.execute(
            "INSERT INTO notifications(event, title, dedupe_key, problem_status) "
            "VALUES ('cloud_preflight_blocked', 'Blocked', 'blocked:installation:preflight', 'open')"
        )
        self.rejects(
            "INSERT INTO notifications(event, title, dedupe_key) "
            "VALUES ('cloud_preflight_blocked', 'Again', 'blocked:installation:preflight')"
        )
        # A resolved problem records when, and only a resolved one does.
        self.rejects(
            "UPDATE notifications SET problem_status = 'resolved'",
            error=psycopg.errors.CheckViolation,
        )
        self.execute("UPDATE notifications SET problem_status = 'resolved', resolved_at = now()")
        self.rejects(
            "INSERT INTO notifications(event, title, resolved_at) VALUES ('x', 'x', now())",
            error=psycopg.errors.CheckViolation,
        )

    def test_vault_targets_are_immutable(self) -> None:
        vault = self.vault()
        self.rejects("UPDATE vault_targets SET bucket = 'other'")
        self.rejects("DELETE FROM vault_targets")
        other = self.execute(
            "INSERT INTO vault_targets(provider, bucket, region, key_layout) "
            "VALUES ('s3', 'other', 'eu-south-1', 'vault_prefix') RETURNING id"
        )[0]["id"]
        self.rejects("UPDATE vaults SET target_id = %s WHERE id = %s", (other, vault["id"]))
        self.rejects("UPDATE vaults SET uuid = %s WHERE id = %s", (uuid.uuid4(), vault["id"]))
        self.rejects("INSERT INTO vault_targets(provider, bucket, region, key_layout) "
                     "VALUES ('gcs', 'b', 'r', 'vault_prefix')")

    def test_vault_bucket_targets(self) -> None:
        """#735: a dedicated target is a frostvault-<slug>-<8> bucket, never the System Bucket."""
        insert = ("INSERT INTO vault_targets(provider, bucket, region, key_layout, retention_lock_days) "
                  "VALUES ('s3', %s, 'eu-south-1', %s, %s) RETURNING id")
        self.execute(insert, ("frostvault-photos-2024-ab12cd34", "bucket_root", None))
        self.execute(insert, ("frostvault-docs-ab12cd35", "bucket_root", 180))
        for bucket, layout, days in (
                ("frostvault-system-ab12cd34", "bucket_root", None),
                ("frostvault-system-photos-ab12cd34", "bucket_root", None),
                ("frostvault-Photos-ab12cd34", "bucket_root", None),
                ("other-photos-ab12cd34", "bucket_root", None),
                ("frostvault-photos-ab12", "bucket_root", None),
                ("frostvault-" + "a" * 50 + "-ab12cd34", "bucket_root", None),
                ("frostvault-photos-ab12cd36", "bucket_root", 0),
                ("shared", "vault_prefix", 30),
                ("shared", "unknown", None)):
            with self.subTest(bucket=bucket, layout=layout, days=days):
                self.rejects(insert, (bucket, layout, days), error=psycopg.errors.CheckViolation)
        self.execute(insert, ("frostvault-vault-system-ab12cd36", "bucket_root", None))
        # The default only filled the targets that existed before 0010.
        self.rejects("INSERT INTO vault_targets(provider, bucket, region) VALUES ('s3', 'legacy', 'r')",
                     error=psycopg.errors.NotNullViolation)
        layout = self.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='vault_targets' AND column_name='key_layout'")[0]
        self.assertIsNone(layout["column_default"])

    def test_a_vault_bucket_target_belongs_to_one_vault(self) -> None:
        """#735: two Vaults never share a bucket_root target; a shared bucket stays shared."""
        dedicated = self.execute(
            "INSERT INTO vault_targets(provider, bucket, region, key_layout) "
            "VALUES ('s3', 'frostvault-docs-aaaaaaaa', 'eu-south-1', 'bucket_root') RETURNING id")[0]["id"]
        first = self.vault("/sources/managed/a", target_id=dedicated, lifecycle="provisioning")
        self.rejects("INSERT INTO vaults(name, source_root, target_id, lifecycle) "
                     "VALUES ('Other', '/sources/managed/b', %s, 'provisioning')", (dedicated,),
                     error=psycopg.errors.UniqueViolation)
        other = self.vault("/sources/managed/c", target_id=self.target_id(), lifecycle="provisioning")
        self.rejects("UPDATE vaults SET target_id=%s WHERE id=%s", (dedicated, other["id"]),
                     error=psycopg.errors.UniqueViolation)
        # The shared target of older Vaults takes any number of Vaults.
        shared = self.vault("/sources/managed/d")
        self.vault("/sources/managed/e")
        self.assertEqual(first["target_id"], dedicated)
        # The upload and rename identity triggers take the key prefix from the target.
        prefixes = self.execute("SELECT vault_key_prefix(%s) AS dedicated, vault_key_prefix(%s) AS shared, "
                                "(SELECT uuid::text FROM vaults WHERE id=%s) AS uuid",
                                (first["id"], shared["id"], shared["id"]))[0]
        self.assertEqual((prefixes["dedicated"], prefixes["shared"]), ("", f"vaults/{prefixes['uuid']}/"))

    def test_only_a_provisioning_vault_changes_its_target(self) -> None:
        """#735: a name another account owns is replaced, only before activation."""
        insert = ("INSERT INTO vault_targets(provider, bucket, region, key_layout) "
                  "VALUES ('s3', %s, 'eu-south-1', 'bucket_root') RETURNING id")
        first = self.execute(insert, ("frostvault-docs-aaaaaaaa",))[0]["id"]
        second = self.execute(insert, ("frostvault-docs-bbbbbbbb",))[0]["id"]
        third = self.execute(insert, ("frostvault-docs-cccccccc",))[0]["id"]
        vault = self.vault(target_id=first, lifecycle="provisioning")
        self.execute("INSERT INTO vault_bucket_provisioning(vault_id, requested_at) VALUES (%s, now())",
                     (vault["id"],))
        self.rejects("UPDATE vaults SET paused = true WHERE id = %s", (vault["id"],),
                     error=psycopg.errors.CheckViolation)
        self.execute("UPDATE vaults SET target_id = %s WHERE id = %s", (second, vault["id"]))
        self.rejects("UPDATE vaults SET target_id = %s, lifecycle = 'active' WHERE id = %s",
                     (third, vault["id"]))
        self.rejects("UPDATE vault_bucket_provisioning SET stage = 'verified'",
                     error=psycopg.errors.CheckViolation)
        self.rejects("UPDATE vault_bucket_provisioning SET verified_at = now()",
                     error=psycopg.errors.CheckViolation)
        self.execute("UPDATE vault_bucket_provisioning SET stage = 'verified', verified_at = now()")
        self.rejects("UPDATE vault_bucket_provisioning SET last_error = 'AccessDenied'",
                     error=psycopg.errors.CheckViolation)
        self.execute("UPDATE vaults SET lifecycle = 'active' WHERE id = %s", (vault["id"],))
        self.rejects("UPDATE vaults SET target_id = %s WHERE id = %s", (third, vault["id"]))
        self.rejects("UPDATE vaults SET lifecycle = 'provisioning' WHERE id = %s", (vault["id"],))

    def test_vault_lifecycle_combinations(self) -> None:
        vault = self.vault()
        self.assertEqual((vault["lifecycle"], vault["paused"]), ("active", False))
        self.execute("UPDATE vaults SET paused = true, lifecycle = 'reconnect_required' WHERE id = %s",
                     (vault["id"],))
        self.rejects("UPDATE vaults SET lifecycle = 'stopping' WHERE id = %s", (vault["id"],))
        self.rejects("UPDATE vaults SET lifecycle = 'unknown', paused = false WHERE id = %s", (vault["id"],))
        self.execute("UPDATE vaults SET lifecycle = 'stopping', paused = false WHERE id = %s", (vault["id"],))
        self.rejects("UPDATE vaults SET lifecycle = 'tombstone' WHERE id = %s", (vault["id"],))
        self.execute(
            "UPDATE vaults SET lifecycle = 'tombstone', root_released_at = now() WHERE id = %s",
            (vault["id"],),
        )
        self.rejects(
            "UPDATE vaults SET lifecycle = 'active', root_released_at = NULL WHERE id = %s",
            (vault["id"],),
        )
        # A released root can be allocated again; an allocated one cannot.
        self.vault()
        self.rejects(
            "INSERT INTO vaults(name, source_root, target_id) VALUES ('Again', %s, %s)",
            ("/sources/managed/a", vault["target_id"]),
        )

    def test_file_pointers_name_versions_of_the_same_file(self) -> None:
        vault = self.vault()
        first, second = (
            self.execute(
                "INSERT INTO files(vault_id, path, path_since) VALUES (%s, %s, now()) RETURNING id",
                (vault["id"], path),
            )[0]["id"]
            for path in ("a.txt", "b.txt")
        )
        self.rejects(
            "INSERT INTO files(vault_id, path, path_since) VALUES (%s, 'a.txt', now())",
            (vault["id"],),
        )
        digest = bytes(32)
        version = self.execute(
            "INSERT INTO archive_versions(file_id, object_key, provider_version_id, size, "
            "plaintext_sha256) VALUES (%s, 'vaults/x/a.txt', 'v1', 1, %s) RETURNING id",
            (first, digest),
        )[0]["id"]
        self.rejects("UPDATE files SET replacing_version_id = %s WHERE id = %s", (version, second))
        # Current is reached only by promoting the verified replacement (#587).
        self.execute("UPDATE files SET replacing_version_id = %s, cloud_since = now() WHERE id = %s",
                     (version, first))
        self.execute("UPDATE archive_versions SET integrity = 'verified', verified_at = now() "
                     "WHERE id = %s", (version,))
        self.execute("UPDATE files SET current_version_id = replacing_version_id, "
                     "replacing_version_id = NULL WHERE id = %s", (first,))
        self.rejects("UPDATE files SET current_version_id = %s WHERE id = %s", (version, second))
        self.rejects("UPDATE files SET replacing_version_id = %s WHERE id = %s", (version, first))
        self.rejects("DELETE FROM archive_versions WHERE id = %s", (version,))
        self.rejects(
            "INSERT INTO archive_versions(file_id, object_key, size, plaintext_sha256) "
            "VALUES (%s, 'k', 1, %s)", (first, b"short"),
        )
        self.rejects(
            "INSERT INTO archive_versions(file_id, object_key, size, availability) "
            "VALUES (%s, 'k', 1, 'purged')", (first,),
        )
        self.rejects(
            "INSERT INTO jobs(vault_id, file_id, archive_version_id, action, status, origin) "
            "VALUES (%s, %s, %s, 'upload', 'queued', 'manual')",
            (vault["id"], second, version),
        )
        self.execute(
            "INSERT INTO jobs(vault_id, file_id, archive_version_id, action, status, origin) "
            "VALUES (%s, %s, %s, 'upload', 'queued', 'automatic')",
            (vault["id"], first, version),
        )


if __name__ == "__main__":
    unittest.main()
