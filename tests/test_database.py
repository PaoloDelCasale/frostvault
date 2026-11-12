"""Baseline schema and the simple schema guard (ADR-0035, #601).

A database is initialized only when empty, used only at ``0001_baseline``, and
otherwise refused before any write: at app start and in metadata restore.
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
    "archive_versions", "jobs",
}


def _tables(path: Path) -> set[str]:
    with PostgresConnection(str(path)) as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {row["tablename"] for row in rows}


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

        self.assertEqual(_tables(self.path), BASELINE_TABLES)
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
        self.assertEqual(_tables(self.path), BASELINE_TABLES)

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
            "INSERT INTO vault_targets(provider, bucket, region) "
            "VALUES ('s3', 'bucket', 'eu-south-1') RETURNING id"
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
        self.assertEqual(tables, BASELINE_TABLES)
        referenced = {row["referenced"] for row in self.execute(
            "SELECT confrelid::regclass::text AS referenced FROM pg_constraint WHERE contype = 'f'"
        )}
        self.assertEqual(referenced, {"vault_targets", "vaults", "files", "archive_versions"})

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
        # The OIDC Identity is bound all at once, never half.
        self.rejects("UPDATE owner SET oidc_subject = 'subject'")
        self.execute(
            "UPDATE owner SET oidc_issuer = 'https://id.example', oidc_subject = 'subject', "
            "oidc_bound_at = now()"
        )

    def test_vault_targets_are_immutable(self) -> None:
        vault = self.vault()
        self.rejects("UPDATE vault_targets SET bucket = 'other'")
        self.rejects("DELETE FROM vault_targets")
        other = self.execute(
            "INSERT INTO vault_targets(provider, bucket, region) "
            "VALUES ('s3', 'other', 'eu-south-1') RETURNING id"
        )[0]["id"]
        self.rejects("UPDATE vaults SET target_id = %s WHERE id = %s", (other, vault["id"]))
        self.rejects("UPDATE vaults SET uuid = %s WHERE id = %s", (uuid.uuid4(), vault["id"]))
        self.rejects("INSERT INTO vault_targets(provider, bucket, region) VALUES ('gcs', 'b', 'r')")

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
        self.execute("UPDATE files SET current_version_id = %s WHERE id = %s", (version, first))
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
