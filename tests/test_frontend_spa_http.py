"""FRONTEND_SPA serving seams (issue #58).

With the flag off the Jinja UI is unchanged; with the flag on FastAPI serves
``frontend/dist`` and falls back to ``index.html`` for non-API routes.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.security import hash_password
from app.sessions import create_session
from tests.test_database import run_alembic


class FrontendSpaHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

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
            '<body><div id="root">spa-shell</div>'
            '<script type="module" src="/assets/index-abc123.js"></script>'
            "</body></html>\n",
            encoding="utf-8",
        )
        (self.dist_dir / "assets" / "index-abc123.js").write_text(
            "console.log('spa');\n",
            encoding="utf-8",
        )

        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE) RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs Archive', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (self.vault_id, self.user_id),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            frontend_spa=False,
            frontend_dist_dir=str(self.dist_dir),
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=self.user_id, auth_method="oidc"
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)

    def _enable_spa(self) -> None:
        object.__setattr__(self.test_settings, "frontend_spa", True)

    def test_root_with_flag_off_serves_jinja(self) -> None:
        """Seam 1: GET / with FRONTEND_SPA=0 → Jinja HTML markers."""
        self._authenticate()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("data-can-operate=", response.text)
        self.assertIn("/static/style.css", response.text)
        self.assertNotIn('id="root"', response.text)

    def test_root_with_flag_on_serves_spa_index(self) -> None:
        """Seam 2: GET / with FRONTEND_SPA=1 → the SPA index.html."""
        self._enable_spa()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="root"', response.text)
        self.assertIn("spa-shell", response.text)
        self.assertNotIn("data-can-operate=", response.text)

    def test_unknown_route_with_flag_on_falls_back_to_spa(self) -> None:
        """Seam 3: GET /some/unknown/route with flag on → index.html, 200."""
        self._enable_spa()
        response = self.client.get("/some/unknown/route")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("spa-shell", response.text)
        self.assertIn('id="root"', response.text)

    def test_api_me_not_swallowed_by_spa_fallback(self) -> None:
        """Seam 4: GET /api/me with flag on → JSON, not the SPA shell."""
        self._enable_spa()
        self._authenticate()
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertEqual(payload["username"], "owner")
        self.assertNotIn("spa-shell", response.text)

    def test_spa_cache_headers_for_assets_and_index(self) -> None:
        """Seam 5: hashed asset → immutable; index.html → no-store."""
        self._enable_spa()
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200, index.text)
        self.assertEqual(index.headers.get("cache-control"), "no-store")

        asset = self.client.get("/assets/index-abc123.js")
        self.assertEqual(asset.status_code, 200, asset.text)
        self.assertEqual(
            asset.headers.get("cache-control"),
            "public, max-age=31536000, immutable",
        )
        self.assertIn("spa", asset.text)
