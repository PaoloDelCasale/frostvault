"""Issue #555: actual diagnostics routes, no lifespan, database or live metrics."""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import diagnostics, main
from app.config import settings
from tests.spa_fixture import write_spa_dist

ROOT = Path(__file__).resolve().parents[1]
# Deliberately fake fixtures, never usable deployment credentials.
SCRAPER_TOKEN = "test-only-scraper-credential"
METRIC_TEXT = "# TYPE worker_up gauge\nworker_up 1\n"


class DiagnosticsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = replace(
            settings,
            metrics_token=SCRAPER_TOKEN,
            cookie_secure=False,
            allowed_hosts="vault.example",
            frontend_dist_dir=str(write_spa_dist(Path(temporary.name))),
        )
        for target in ("app.main.settings", "app.diagnostics.settings"):
            self.enterContext(patch(target, self.config))
        # Any unexpected Session lookup or application DB access fails the test.
        self.database = self.enterContext(patch("app.main.db", side_effect=AssertionError("No DB expected")))
        self.render = self.enterContext(patch("app.main.metrics_service.render_prometheus", return_value=METRIC_TEXT))
        # No context-manager lifespan: never initialize DB, workers or cloud SDK.
        self.client = TestClient(main.app, base_url="http://vault.example")
        self.addCleanup(self.client.close)

    def assert_closed(self, response, status: int) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertNotIn(METRIC_TEXT, response.text)
        self.assertNotIn(SCRAPER_TOKEN, response.text)
        self.assertNotIn("text/html", response.headers["content-type"])
        self.render.assert_not_called()
        self.database.assert_not_called()

    def test_metrics_disabled_without_token_even_with_bearer_or_session(self) -> None:
        for configured in ("", " \t "):
            with self.subTest(configured=bool(configured)), patch(
                "app.diagnostics.settings", replace(self.config, metrics_token=configured)
            ):
                for headers in ({}, {"Authorization": f"Bearer {SCRAPER_TOKEN}"},
                                {"Cookie": "frostvault_session=test-only-session"}):
                    response = self.client.get("/metrics", headers=headers)
                    self.assert_closed(response, 404)
                    self.assertEqual(response.json(), {"detail": "Not Found"})
                    self.assertEqual(response.headers["cache-control"], "no-store")

    def test_metrics_require_valid_single_bearer_header(self) -> None:
        for authorization in (
            None, "", "Bearer", "Bearer ", "Basic test-only", "Token test-only",
            "Bearer wrong-test-only-credential", f"Bearer {SCRAPER_TOKEN} extra",
            f"Bearer\t{SCRAPER_TOKEN}",
            f"Bearer {SCRAPER_TOKEN},Bearer {SCRAPER_TOKEN}",
            f"Bearer {SCRAPER_TOKEN}\t", "Bearer bad%value",
        ):
            with self.subTest(authorization=authorization):
                headers = {} if authorization is None else {"Authorization": authorization}
                response = self.client.get("/metrics", headers=headers)
                self.assert_closed(response, 401)
                self.assertEqual(response.json(), {"detail": "Invalid metrics credential"})
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_duplicate_and_non_ascii_credentials_fail_without_500_or_leakage(self) -> None:
        for headers in (
            [("Authorization", f"Bearer {SCRAPER_TOKEN}")] * 2,
            [("Authorization", f"Bearer {SCRAPER_TOKEN}"), ("Authorization", "Bearer wrong-test-only")],
            [(b"Authorization", b"Bearer non-ascii-\xff")],
        ):
            with self.subTest(headers=headers):
                self.assert_closed(self.client.get("/metrics", headers=headers), 401)
        with patch("app.diagnostics.settings", replace(self.config, metrics_token="invalid-config-\u00e8")):
            self.assert_closed(self.client.get("/metrics", headers={"Authorization": "Bearer test-only"}), 401)

    def test_session_and_query_string_are_not_scraper_authentication(self) -> None:
        main.app.dependency_overrides[main.current_user] = lambda: {"id": 1}
        self.addCleanup(main.app.dependency_overrides.pop, main.current_user)
        self.assert_closed(self.client.get("/metrics", headers={
            "Cookie": "frostvault_session=test-only-session",
        }), 401)
        self.assert_closed(self.client.get("/metrics", params={"token": SCRAPER_TOKEN}), 401)

    def test_valid_bearer_uses_constant_time_comparison_and_no_store(self) -> None:
        with patch("app.diagnostics.secrets.compare_digest", wraps=diagnostics.secrets.compare_digest) as compare:
            response = self.client.get("/metrics", headers={"Authorization": f"Bearer {SCRAPER_TOKEN}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, METRIC_TEXT)
        self.assertTrue(response.headers["content-type"].startswith("text/plain; version=0.0.4"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        compare.assert_called_once_with(SCRAPER_TOKEN.encode(), SCRAPER_TOKEN.encode())
        self.render.assert_called_once_with()
        self.database.assert_not_called()

    def test_bearer_scheme_is_case_insensitive_and_accepts_rfc_spaces(self) -> None:
        for scheme in ("bEaReR ", "Bearer  "):
            with self.subTest(scheme=scheme):
                response = self.client.get("/metrics", headers={"Authorization": f"{scheme}{SCRAPER_TOKEN}"})
                self.assertEqual(response.status_code, 200)

    def test_rejected_headers_never_enter_logs_or_settings_repr(self) -> None:
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        supplied = "test-only-invalid-submitted-credential"
        response = self.client.get("/metrics", headers={"Authorization": f"Bearer {supplied}"})
        self.assert_closed(response, 401)
        for value in (SCRAPER_TOKEN, supplied):
            self.assertNotIn(value, output.getvalue())
            self.assertNotIn(value, response.text)
            self.assertNotIn(value, repr(self.config))

    def test_metrics_still_obey_host_and_origin_controls(self) -> None:
        response = self.client.get("/metrics", headers={
            "Host": "evil.example", "Authorization": f"Bearer {SCRAPER_TOKEN}",
        })
        self.assert_closed(response, 400)
        response = self.client.post("/metrics", headers={
            "Origin": "http://evil.example", "Authorization": f"Bearer {SCRAPER_TOKEN}",
        })
        self.assert_closed(response, 403)

    def test_diagnostics_subpaths_never_fall_back_to_spa(self) -> None:
        for path in ("/metrics/", "/metrics/unknown", "/docs/unknown", "/redoc/unknown", "/openapi.json/unknown"):
            with self.subTest(path=path):
                self.assert_closed(self.client.get(path), 404)
        self.assertEqual(self.client.get("/client-route").status_code, 200)
        self.assertIn("text/html", self.client.get("/client-route").headers["content-type"])

    def test_public_readiness_omits_workload_but_preserves_each_check(self) -> None:
        checks = {"database": True, "worker": True, "config": True, "sources_layout": True}
        for failed in (None, *checks):
            current = {name: name != failed for name in checks}
            status = "ready" if failed is None else "not_ready"
            report = {
                "status": status, "checks": current,
                "queue": {"stalled": True, "queued": 789, "seconds_without_progress": 9999},
                "future_internal_diagnostic": "not public",
            }
            with self.subTest(failed=failed), patch("app.main.health_service.readiness_report", return_value=report):
                response = self.client.get("/ready")
                self.assertEqual(response.status_code, 200 if failed is None else 503)
                self.assertEqual(response.json(), {"status": status, "checks": current})
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.render.assert_not_called()
        self.database.assert_not_called()


class DocsAndExporterTests(unittest.TestCase):
    def test_fresh_app_default_closed_explicit_development_opt_in_and_export(self) -> None:
        script = textwrap.dedent('''
            import json
            import subprocess
            import sys
            from dataclasses import replace
            from pathlib import Path
            from unittest.mock import patch
            from fastapi.testclient import TestClient
            from app import main
            from tests.spa_fixture import write_spa_dist
            directory = Path(sys.argv[1])
            enabled = sys.argv[2] == "true"
            assert main.settings.api_docs_enabled == enabled
            assert not main.settings.metrics_token
            with patch("app.main.settings", replace(main.settings, frontend_dist_dir=str(write_spa_dist(directory)))), patch("app.main.db", side_effect=AssertionError("No DB")):
                client = TestClient(main.app)
                for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
                    response = client.get(path)
                    assert response.status_code == (200 if enabled else 404), (path, response.text)
                    if not enabled:
                        assert response.json() == {"detail": "Not Found"}
                for path in ("/docs/", "/redoc/", "/openapi.json/"):
                    response = client.get(path)
                    assert response.status_code == 404, (path, response.text)
                schema = main.app.openapi()
                assert "/api/me" in schema["paths"]
                if enabled:
                    assert client.get("/openapi.json").json() == schema
                output = directory / "export.json"
                subprocess.run([sys.executable, "scripts/export_openapi.py", str(output)], check=True)
                assert json.loads(output.read_text()) == schema
                client.close()
        ''')
        with tempfile.TemporaryDirectory() as temporary:
            for flag in (None, "false", "true"):
                environment = {"PATH": os.environ.get("PATH", ""), "AWS_EC2_METADATA_DISABLED": "true"}
                if flag is not None:
                    environment["API_DOCS_ENABLED"] = flag
                with self.subTest(flag=flag):
                    result = subprocess.run(
                        [sys.executable, "-c", script, temporary, str(flag == "true").lower()],
                        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_programmatic_metrics_schema_documents_independent_bearer(self) -> None:
        schema = main.app.openapi()
        self.assertEqual(schema["paths"]["/metrics"]["get"]["security"], [{"MetricsBearer": []}])
        bearer = schema["components"]["securitySchemes"]["MetricsBearer"]
        self.assertEqual(bearer["type"], "http")
        self.assertEqual(bearer["scheme"], "bearer")
        self.assertNotIn(SCRAPER_TOKEN, json.dumps(schema))


if __name__ == "__main__":
    unittest.main()
