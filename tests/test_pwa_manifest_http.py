"""PWA web app manifest installability (issue #72, seam 1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.test_database import run_alembic


class PwaManifestHttpTests(unittest.TestCase):
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
        (self.dist_dir / "pwa-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.dist_dir / "pwa-512.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        manifest = {
            "name": "FrostVault",
            "short_name": "FrostVault",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#f4f7f4",
            "theme_color": "#257a4b",
            "icons": [
                {
                    "src": "/pwa-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/pwa-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
            ],
        }
        (self.dist_dir / "manifest.webmanifest").write_text(
            json.dumps(manifest), encoding="utf-8"
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
        self._patcher = patch("app.main.settings", self._settings)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self._db_patcher = patch("app.database.settings", self._settings)
        self._db_patcher.start()
        self.addCleanup(self._db_patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def test_manifest_is_served_and_meets_installability_requirements(self) -> None:
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        content_type = response.headers.get("content-type", "")
        self.assertTrue(
            "manifest" in content_type or "json" in content_type,
            content_type,
        )
        payload = response.json()
        self.assertEqual(payload["name"], "FrostVault")
        self.assertEqual(payload["short_name"], "FrostVault")
        self.assertEqual(payload["start_url"], "/")
        self.assertIn(payload["display"], {"standalone", "minimal-ui", "fullscreen"})
        icons = payload["icons"]
        self.assertGreaterEqual(len(icons), 1)
        sizes = {icon.get("sizes") for icon in icons}
        self.assertTrue(
            any(size in sizes for size in {"192x192", "512x512"}),
            sizes,
        )
        for icon in icons:
            icon_response = self.client.get(icon["src"])
            self.assertEqual(icon_response.status_code, 200, icon["src"])


if __name__ == "__main__":
    unittest.main()
