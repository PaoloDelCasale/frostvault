"""Removed admin surfaces and deployment-only configuration."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import main, storage

from app.services.metadata_backups import build_config_snapshot
from tests.fixtures import http_client, override_settings


class EssentialAdministrationTests(unittest.TestCase):
    def test_removed_apis_are_not_in_openapi_or_callable(self):
        paths = ("/api/audit-events", "/api/admin/audit-events", "/api/admin/worker-errors", "/api/admin/settings")
        schema = main.app.openapi()
        client = http_client()
        for path in paths:
            self.assertNotIn(path, schema["paths"])
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.patch("/api/admin/settings", json={}).status_code, 404)
        for path in ("/api/general/metadata-backups", "/api/general/source-volumes"):
            self.assertIn(path, schema["paths"])
            self.assertEqual(client.get(path).status_code, 401)

    def test_configuration_consumers_never_query_database_overrides(self):
        with (
            override_settings(restore_tier="Standard", scan_interval=1234) as configured,
            patch("app.main.db", side_effect=AssertionError("no DB resolution")),
            patch("app.storage.db", side_effect=AssertionError("no DB resolution")),
        ):
            self.assertIs(main.settings, configured)
            self.assertIs(storage.settings, configured)
            snapshot = build_config_snapshot(configured)
            self.assertEqual(snapshot["scan_interval"], 1234)
            self.assertEqual(snapshot["restore_tier"], "Standard")
            self.assertTrue({"archive_master_key", "oidc_client_secret", "bootstrap_admin_password", "smtp_password"}.isdisjoint(snapshot))

