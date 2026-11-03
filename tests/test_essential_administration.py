"""Removed admin surfaces, deployment-only configuration and migration safety."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, storage
from app.config import settings

from app.services.metadata_backups import build_config_snapshot
from tests.essential_administration_migration_contract import assert_administration_migration
from tests.test_database import run_alembic


class EssentialAdministrationTests(unittest.TestCase):
    def test_removed_apis_are_not_in_openapi_or_callable(self):
        paths = ("/api/audit-events", "/api/admin/audit-events", "/api/admin/worker-errors", "/api/admin/settings")
        schema = main.app.openapi()
        client = TestClient(main.app)
        for path in paths:
            self.assertNotIn(path, schema["paths"])
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.patch("/api/admin/settings", json={}).status_code, 404)
        for path in ("/api/admin/metadata-backups", "/api/admin/source-volumes"):
            self.assertIn(path, schema["paths"])
            self.assertEqual(client.get(path).status_code, 401)

    def test_configuration_consumers_never_query_database_overrides(self):
        configured = replace(settings, restore_tier="Standard", scan_interval=1234)
        with (
            patch("app.main.settings", configured),
            patch("app.storage.settings", configured),
            patch("app.main.db", side_effect=AssertionError("no DB resolution")),
            patch("app.storage.db", side_effect=AssertionError("no DB resolution")),
        ):
            self.assertIs(main._runtime_settings(), configured)
            self.assertIs(storage._runtime_settings(), configured)
            snapshot = build_config_snapshot(configured)
            self.assertEqual(snapshot["scan_interval"], 1234)
            self.assertEqual(snapshot["restore_tier"], "Standard")
            self.assertTrue({"archive_master_key", "oidc_client_secret", "bootstrap_admin_password", "smtp_password"}.isdisjoint(snapshot))

    def test_migration_preserves_sessions_jobs_and_the_only_cloud_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.db"
            assert_administration_migration(
                self, lambda target, command="upgrade": run_alembic(path, target, command=command),
                lambda: PostgresConnection(str(path)),
            )
            with PostgresConnection(str(path)) as connection:
                tables = {r["name"] for r in connection.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema()").fetchall()}
            self.assertTrue({"system_settings", "worker_errors"}.isdisjoint(tables))
