"""Catalog preflight rejects ambiguity before any catalog or backup mutation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import backup_upgrade, database, migrate_on_start
from app.schema_state import DatabaseSchemaError, read_schema_revision


class SchemaStateTests(unittest.TestCase):
    def connection(self, *, present=False, revisions=(), nonempty=False, tuples=False):
        c = MagicMock()
        def execute(sql, *args):
            result = MagicMock()
            if "AS present" in sql:
                result.fetchone.return_value = (present,) if tuples else {"present": present}
            elif "AS nonempty" in sql:
                result.fetchone.return_value = (nonempty,) if tuples else {"nonempty": nonempty}
            elif sql == "SELECT version_num FROM alembic_version":
                result.fetchall.return_value = [(r,) if tuples else {"version_num": r} for r in revisions]
            else:
                raise AssertionError(f"Unexpected SQL during read-only preflight: {sql}")
            return result
        c.execute.side_effect = execute
        return c

    def test_only_a_truly_empty_unversioned_database_is_fresh(self):
        for present in (False, True):
            for tuples in (False, True):
                with self.subTest(present=present, tuples=tuples):
                    c = self.connection(present=present, tuples=tuples)
                    self.assertIsNone(read_schema_revision(c))
                    c = self.connection(present=present, nonempty=True, tuples=tuples)
                    with self.assertRaisesRegex(DatabaseSchemaError, "Unversioned nonempty"):
                        read_schema_revision(c)
                    self.assertTrue(all(call.args[0].lstrip().startswith("SELECT")
                                        for call in c.execute.call_args_list))

    def test_multiple_or_invalid_revisions_fail_closed_with_both_row_factories(self):
        for tuples in (False, True):
            for revisions in (("known", "other"), ("",), (None,), ("  ",)):
                with self.subTest(revisions=revisions, tuples=tuples):
                    with self.assertRaises(DatabaseSchemaError):
                        read_schema_revision(self.connection(present=True, revisions=revisions, tuples=tuples))
            self.assertEqual(read_schema_revision(self.connection(
                present=True, revisions=("known",), tuples=tuples)), "known")

    def test_runtime_refuses_divergent_catalog_before_bootstrap(self):
        c = self.connection(present=True, revisions=(database.HEAD_SCHEMA_REVISION, "other"))
        c.__enter__.return_value = c
        with patch.object(database, "db", return_value=c), patch.object(database, "_bootstrap_owner") as bootstrap:
            with self.assertRaisesRegex(DatabaseSchemaError, "divergent"):
                database.initialize_database()
            bootstrap.assert_not_called()

    def test_startup_refuses_unversioned_content_before_backup_or_alembic(self):
        c = self.connection(nonempty=True)
        c.__enter__.return_value = c
        with patch.object(migrate_on_start, "db", return_value=c), \
                patch.object(migrate_on_start, "settings", SimpleNamespace(auto_migrate=True)), \
                patch.object(migrate_on_start, "_upgrade_existing_with_backup") as backup, \
                patch.object(migrate_on_start, "_alembic_upgrade") as upgrade:
            with self.assertRaisesRegex(migrate_on_start.SchemaMigrationError, "Unversioned nonempty"):
                migrate_on_start.ensure_schema_current()
            backup.assert_not_called()
            upgrade.assert_not_called()

    def test_manual_upgrade_rejects_bad_catalogs_before_backup_or_provider(self):
        for c in (self.connection(nonempty=True),
                  self.connection(present=True, revisions=("unknown",)),
                  self.connection(present=True, revisions=("known", "other"))):
            c.__enter__.return_value = c
            with patch.object(backup_upgrade, "db", return_value=c), \
                    patch.object(backup_upgrade.metadata_backups, "run_pre_upgrade_backup") as backup, \
                    patch.object(backup_upgrade.metadata_backups, "default_object_store") as provider, \
                    patch.object(backup_upgrade.subprocess, "run") as upgrade:
                self.assertEqual(backup_upgrade.main([]), 2)
                backup.assert_not_called()
                provider.assert_not_called()
                upgrade.assert_not_called()

    def test_manual_upgrade_does_not_dump_a_fresh_database(self):
        for flags, result in (([], 0), (["--skip-upgrade"], 2)):
            c = self.connection()
            c.__enter__.return_value = c
            with patch.object(backup_upgrade, "db", return_value=c), \
                    patch.object(backup_upgrade.metadata_backups, "run_pre_upgrade_backup") as backup, \
                    patch.object(backup_upgrade.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as upgrade:
                self.assertEqual(backup_upgrade.main(flags), result)
                backup.assert_not_called()
                self.assertEqual(upgrade.call_count, int(not flags))


class SchemaStatePostgresTests(unittest.TestCase):
    """Real PostgreSQL/Alembic entrypoint contracts, exercised in PR CI."""

    def setUp(self):
        from tests.postgres import PostgresConnection
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preflight"
        self.connect = lambda: PostgresConnection(self.path)

    def test_empty_table_is_not_a_fresh_database_for_alembic(self):
        from tests.postgres import run_alembic
        with self.connect() as c:
            c.execute("CREATE TABLE retained_evidence(id integer PRIMARY KEY)")
        result = run_alembic(self.path)
        self.assertNotEqual(result.returncode, 0)
        with self.connect() as c:
            self.assertIsNotNone(c.execute("SELECT to_regclass('retained_evidence') AS r").fetchone()["r"])
            self.assertIsNone(c.execute("SELECT to_regclass('alembic_version') AS r").fetchone()["r"])
            self.assertIsNone(c.execute("SELECT to_regclass('vaults') AS r").fetchone()["r"])

    def test_other_schema_routines_and_types_are_not_a_fresh_database(self):
        for ddl in ("CREATE SCHEMA other; CREATE TABLE other.retained_evidence(id integer)",
                    "CREATE FUNCTION retained_evidence() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
                    "CREATE TYPE retained_evidence AS ENUM ('keep')"):
            with self.subTest(ddl=ddl), self.connect() as c:
                c.execute(ddl)
                with self.assertRaisesRegex(DatabaseSchemaError, "Unversioned nonempty"):
                    read_schema_revision(c)
                c.rollback()

    def test_empty_alembic_bookkeeping_is_allowed_but_not_with_content(self):
        with self.connect() as c:
            c.execute("CREATE TABLE alembic_version(version_num varchar(64) PRIMARY KEY)")
            self.assertIsNone(read_schema_revision(c))
            c.execute("CREATE TABLE retained_evidence(id integer)")
            with self.assertRaisesRegex(DatabaseSchemaError, "Unversioned nonempty"):
                read_schema_revision(c)

    def test_unknown_revision_direct_alembic_is_read_only(self):
        from tests.postgres import run_alembic
        with self.connect() as c:
            c.execute("CREATE TABLE alembic_version(version_num varchar(64) PRIMARY KEY)")
            c.execute("INSERT INTO alembic_version VALUES ('unknown')")
            c.execute("CREATE TABLE retained_evidence(id integer)")
            c.execute("INSERT INTO retained_evidence VALUES (42)")
        result = run_alembic(self.path)
        self.assertNotEqual(result.returncode, 0)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "unknown")
            self.assertEqual(c.execute("SELECT id FROM retained_evidence").fetchone()["id"], 42)
            self.assertIsNone(c.execute("SELECT to_regclass('vaults') AS r").fetchone()["r"])


if __name__ == "__main__":
    unittest.main()
