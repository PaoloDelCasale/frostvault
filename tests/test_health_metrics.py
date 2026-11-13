"""Liveness, readiness, and Prometheus metrics (issue #16).

Seams under test:
- ``GET /health`` — process liveness
- ``GET /ready`` — dependency-aware readiness (DB / worker / config)
- ``GET /metrics`` — Prometheus text exposition without high-cardinality labels
"""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings

from tests.owner_fixtures import insert_owner
from app.services import health as health_service
from app.services import source_layout
from app.services import metrics as metrics_service
from tests.test_database import run_alembic


class HealthAndMetricsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with PostgresConnection(str(self.database_path)) as connection:
            insert_owner(connection, display_name="Admin", password="admin-password-1")

        self.test_settings = replace(
            settings,
            database_dsn=database_dsn(str(self.database_path)),
            cookie_secure=False,
            allowed_hosts="",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.health.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch("app.services.source_layout.path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)
        source_layout.prepare_sources_layout()

        health_service.mark_worker_heartbeat()
        metrics_service.reset_for_tests()
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    def test_liveness_stays_ok_even_when_dependencies_are_down(self) -> None:
        with patch(
            "app.services.health.check_database",
            return_value=False,
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok")

    def test_readiness_reports_not_ready_when_database_is_unavailable(self) -> None:
        with patch(
            "app.services.health.check_database",
            return_value=False,
        ):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertFalse(body["checks"]["database"])

    def test_readiness_ok_when_database_worker_and_config_are_healthy(self) -> None:
        health_service.mark_worker_heartbeat()
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["checks"]["database"])
        self.assertTrue(body["checks"]["worker"])
        self.assertTrue(body["checks"]["config"])
        self.assertNotIn("crypt_custody", body)

    def test_upload_health_is_informational_and_public_readiness_exposes_no_workload(self) -> None:
        from app.services import upload_pipeline, upload_remediation
        coordinator = upload_pipeline._Pipeline(1)
        coordinator.journal_failed("private-intent", "private-fact-hash")
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("DELETE FROM upload_remediation_sweep")
        with patch.dict(upload_pipeline._registry, {self.test_settings.database_dsn: coordinator}):
            upload_remediation.observe(main.db)
            report = health_service.readiness_report()
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(report["upload_journal"], {"held": True, "uncertain_facts": 1})
        self.assertTrue(report["upload_remediation"]["stale"])
        self.assertTrue(report["upload_remediation"]["missing_ticks"])
        self.assertEqual(set(response.json()), {"status", "checks"})
        self.assertNotIn("private-intent", response.text)
        self.assertNotIn("private-fact-hash", response.text)
        self.assertNotIn(self.test_settings.database_dsn, response.text)

    def test_metrics_expose_low_cardinality_prometheus_counters(self) -> None:
        metrics_service.inc("jobs_completed_total", action="upload")
        metrics_service.inc("verification_failures_total")
        metrics_service.inc("notification_deliveries_total", channel="email", result="failed")
        metrics_service.set_gauge("worker_up", 1)

        # Scrapers use their own explicit credential, not an Owner Session.
        with patch("app.diagnostics.settings", replace(settings, metrics_token="test-only-metrics-credential")):
            response = self.client.get("/metrics", headers={
                "Authorization": "Bearer test-only-metrics-credential",
            })
        self.assertEqual(response.status_code, 200, response.text)
        text = response.text
        self.assertIn("# TYPE jobs_completed_total counter", text)
        self.assertIn('jobs_completed_total{action="upload"}', text)
        self.assertIn("verification_failures_total", text)
        self.assertIn('notification_deliveries_total{channel="email",result="failed"}', text)
        self.assertIn("worker_up", text)
        # High-cardinality / sensitive labels must never appear.
        self.assertNotIn("path=", text)
        self.assertNotIn("username=", text)
        self.assertNotIn("user=", text)


class MetricsLabelGuardTests(unittest.TestCase):
    def test_metrics_reject_sensitive_label_names(self) -> None:
        with self.assertRaises(ValueError):
            metrics_service.inc("jobs_completed_total", path="/secret/file.txt")
        with self.assertRaises(ValueError):
            metrics_service.inc("jobs_completed_total", username="alice")


if __name__ == "__main__":
    unittest.main()
