"""Liveness, readiness, and Prometheus metrics (issue #16).

Seams under test:
- ``GET /health`` — process liveness
- ``GET /ready`` — dependency-aware readiness (DB / worker / config)
- ``GET /metrics`` — Prometheus text exposition without high-cardinality labels
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import main
from app.services import health as health_service
from app.services import source_layout
from app.services import metrics as metrics_service
from tests.fixtures import FrostVaultTestCase, override_settings


class HealthAndMetricsHttpTests(FrostVaultTestCase):
    owner = {"display_name": "Admin", "password": "admin-password-1"}
    settings_overrides = {"allowed_hosts": ""}

    def setUp(self) -> None:
        super().setUp()
        self.sources_root = self.tmp_path / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.enterContext(patch("app.services.source_layout.path_is_mount", return_value=True))
        source_layout.prepare_sources_layout()

        health_service.mark_worker_heartbeat()
        metrics_service.reset_for_tests()
        self.client = self.http_client()

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
        with self.database.connect() as connection:
            connection.execute("DELETE FROM upload_remediation_sweep")
        with patch.dict(upload_pipeline._registry, {self.settings.database_dsn: coordinator}):
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
        self.assertNotIn(self.settings.database_dsn, response.text)

    def test_metrics_expose_low_cardinality_prometheus_counters(self) -> None:
        metrics_service.inc("jobs_completed_total", action="upload")
        metrics_service.inc("verification_failures_total")
        metrics_service.inc("notification_deliveries_total", channel="email", result="failed")
        metrics_service.set_gauge("worker_up", 1)

        # Scrapers use their own explicit credential, not an Owner Session.
        with override_settings(metrics_token="test-only-metrics-credential"):
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
