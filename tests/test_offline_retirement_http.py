"""The legacy Worker URL retires existing installations without cache reuse."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.test_database import run_alembic


class OfflineRetirementHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.dist_dir = Path(self._tmp.name) / "dist"
        self.dist_dir.mkdir()
        (self.dist_dir / "assets").mkdir()
        (self.dist_dir / "index.html").write_text(
            "<!doctype html><html><head><title>FrostVault</title></head>"
            '<body><div id="root">spa</div></body></html>\n',
            encoding="utf-8",
        )
        (self.dist_dir / "sw.js").write_text(
            (Path(__file__).resolve().parents[1] / "frontend/public/sw.js").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        self._settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            frontend_dist_dir=str(self.dist_dir),
            cookie_secure=False,
            allowed_hosts="",
            trusted_proxies="",
            oidc_enabled=False,
        )
        for target in ("app.main.settings", "app.database.settings"):
            patcher = patch(target, self._settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def test_retirement_script_is_served_without_auth_and_without_stale_caching(self) -> None:
        response = self.client.get("/sw.js?old-version=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers.get("content-type", ""))
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertIn("self.registration.unregister()", response.text)
        self.assertNotIn("precacheAndRoute", response.text)
        self.assertNotIn('addEventListener("fetch"', response.text)

    def test_removed_manifest_and_icons_do_not_fall_back_to_html(self) -> None:
        for path in ("/manifest.webmanifest", "/pwa-192.png", "/pwa-512.png"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
