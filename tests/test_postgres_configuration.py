"""PostgreSQL-only configuration and backup boundary regressions (#422)."""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.services import metadata_backups
from tests import postgres


class PostgreSQLConfigurationTests(unittest.TestCase):
    def test_retired_backend_options_are_not_settings(self):
        names = {field.name for field in fields(config.Settings)}
        self.assertNotIn("db_backend", names)
        self.assertNotIn("sqlite_path", names)
        self.assertFalse(hasattr(database, "SQLiteConnection"))

    def test_database_uses_native_psycopg_and_pg_environment_by_default(self):
        self.addCleanup(database.close_pools)
        with patch.object(database, "settings", replace(config.settings, database_dsn="")):
            pool = database._pool_for("")
        self.assertEqual(pool.conninfo, "")
        self.assertEqual(pool.kwargs["row_factory"], database.dict_row)
        self.assertEqual(pool.min_size, 0)
        with patch.object(database.psycopg, "connect") as connect:
            database._direct_connection("")
        self.assertEqual(connect.call_args.args, ("",))
        self.assertEqual(connect.call_args.kwargs["row_factory"], database.dict_row)

    def test_invalid_non_postgresql_dsn_is_rejected_without_echoing_it(self):
        with patch.object(config, "settings", replace(config.settings, database_dsn="sqlite:///not-supported")):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL connection string") as caught:
                config.validate_settings()
        self.assertNotIn("not-supported", str(caught.exception))

    def test_database_credentials_stay_out_of_snapshot_and_process_arguments(self):
        dsn = "host=127.0.0.1 port=55432 dbname=test_db user=test_role password=test-only-value"
        cfg = replace(config.settings, database_dsn=dsn)
        self.assertNotIn("database_dsn", metadata_backups.build_config_snapshot(cfg))
        with patch.object(metadata_backups, "settings", cfg), patch.object(metadata_backups.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"custom-format-dump"
            self.assertEqual(metadata_backups._dump_postgres_bytes(), b"custom-format-dump")
        self.assertNotIn(dsn, repr(run.call_args.args))
        self.assertEqual(run.call_args.kwargs["env"]["PGDATABASE"], "test_db")
        self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "test-only-value")

    def test_database_fixtures_fail_without_explicit_test_server(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TEST_POSTGRES_URL is required"):
                postgres._server_dsn()

    def test_legacy_or_unknown_backup_format_is_refused_before_restore(self):
        for manifest in ({}, {"backend": "sqlite", "schema_format": "sqlite-file"}, {"backend": "postgresql", "schema_format": "unknown"}):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as work:
                with patch.object(metadata_backups, "decrypt_backup_file", return_value={"manifest": manifest, "database": b"legacy"}), patch.object(metadata_backups, "_verify_postgres_dump_isolated") as restore:
                    with self.assertRaisesRegex(metadata_backups.BackupError, "Only PostgreSQL"):
                        metadata_backups.verify_restore_isolated("unused", work_dir=Path(work))
                    restore.assert_not_called()
