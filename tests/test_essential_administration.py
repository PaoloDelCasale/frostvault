"""Removed admin surfaces and deployment-only configuration."""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, storage
from app.config import settings

from app.services.metadata_backups import build_config_snapshot


class EssentialAdministrationTests(unittest.TestCase):
    def test_removed_apis_are_not_in_openapi_or_callable(self):
        paths = ("/api/audit-events", "/api/admin/audit-events", "/api/admin/worker-errors", "/api/admin/settings")
        schema = main.app.openapi()
        client = TestClient(main.app)
        for path in paths:
            self.assertNotIn(path, schema["paths"])
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.patch("/api/admin/settings", json={}).status_code, 404)
        for path in ("/api/general/metadata-backups", "/api/general/source-volumes"):
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

