"""Liveness, readiness, and Prometheus metrics (issue #16).

Seams under test:
- ``GET /health`` — process liveness
- ``GET /ready`` — dependency-aware readiness (DB / worker / config)
- ``GET /metrics`` — Prometheus text exposition without high-cardinality labels
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
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

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', %s, TRUE)
                """,
                (hash_password("admin-password-1"),),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
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

    def test_metrics_expose_low_cardinality_prometheus_counters(self) -> None:
        metrics_service.inc("jobs_completed_total", action="upload")
        metrics_service.inc("verification_failures_total")
        metrics_service.inc("notification_deliveries_total", channel="webhook", result="failed")
        metrics_service.set_gauge("worker_up", 1)

        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        text = response.text
        self.assertIn("# TYPE jobs_completed_total counter", text)
        self.assertIn('jobs_completed_total{action="upload"}', text)
        self.assertIn("verification_failures_total", text)
        self.assertIn('notification_deliveries_total{channel="webhook",result="failed"}', text)
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
