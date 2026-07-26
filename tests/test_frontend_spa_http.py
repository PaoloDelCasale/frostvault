"""SPA is the only frontend (issue #71 cut-over).

No FRONTEND_SPA flag: HTML routes always serve frontend/dist; API routes stay
JSON.
"""

from __future__ import annotations

import os
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

REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths the cut-over deletes; source must not keep referencing them.
_DELETED_PATH_FRAGMENTS = (
    "app/templates/",
    "app/templates\\",
    "app/static/app.js",
    "app/static/admin.js",
    "app/static/vault_access.js",
    "app/static/vault_create.js",
    "app/static/style.css",
    "templates/index.html",
    "templates/login.html",
    "templates/no_vault.html",
    "templates/vault_access.html",
    "templates/vault_create.html",
    "templates/admin.html",
)

_SOURCE_SCAN_ROOTS = (
    REPO_ROOT / "app",
    REPO_ROOT / "frontend" / "src",
    REPO_ROOT / "frontend" / "tests",
    REPO_ROOT / "frontend" / "e2e",
    REPO_ROOT / "tests",
    REPO_ROOT / "docs",
)

_SOURCE_SCAN_FILES = (
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / ".env.example",
    REPO_ROOT / ".env.local.example",
    REPO_ROOT / "Dockerfile",
    REPO_ROOT / ".github" / "workflows" / "migrations.yml",
    REPO_ROOT / ".github" / "scripts" / "agent_pipeline.py",
)

_SOURCE_SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".md", ".yml", ".yaml", ".example", ".css", ".html", ".json"}

# Historical ADRs before the cut-over may still describe Jinja; exclude them.
_SKIP_PATH_PARTS = {
    "node_modules",
    "dist",
    "__pycache__",
    ".venv",
    "package-lock.json",
}


def _iter_source_files():
    for root in _SOURCE_SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_PATH_PARTS for part in path.parts):
                continue
            # Pre-cut-over ADRs keep historical Jinja wording.
            if path.parent.name == "adr" and path.name.startswith("000") and path.name < "0007":
                continue
            if path.suffix.lower() not in _SOURCE_SCAN_SUFFIXES and path.name not in {
                "AGENTS.md",
                "README.md",
            }:
                if ".env" not in path.name:
                    continue
            yield path
    for path in _SOURCE_SCAN_FILES:
        if path.is_file():
            yield path


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
            self.operator_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('operator', 'Operator', %s, FALSE) RETURNING id
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
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'operator')",
                (self.vault_id, self.operator_id),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
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

    def _authenticate(self, user_id: int | None = None) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection,
                user_id=user_id if user_id is not None else self.user_id,
                auth_method="oidc",
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)

    def test_root_serves_spa_with_no_flag(self) -> None:
        """Seam 1: GET / returns the SPA with no flag set anywhere."""
        self.assertFalse(hasattr(self.test_settings, "frontend_spa"))
        self.assertNotIn("FRONTEND_SPA", os.environ)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="root"', response.text)
        self.assertIn("spa-shell", response.text)
        self.assertNotIn("data-can-operate=", response.text)
        self.assertNotIn("/static/style.css", response.text)

    def test_html_routes_serve_spa(self) -> None:
        """Seam 2: login / vaults/new / vault/access / admin serve the SPA."""
        for path in ("/login", "/vaults/new", "/vault/access", "/admin"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"{path}: {response.text}")
            self.assertIn("spa-shell", response.text, path)
            self.assertIn('id="root"', response.text, path)

    def test_role_gating_still_enforced_on_api(self) -> None:
        """Seam 2: role gating still redirects/forbids correctly on the API."""
        self._authenticate(self.operator_id)
        denied = self.client.get("/api/vault/quotas")
        self.assertEqual(denied.status_code, 403, denied.text)

        self._authenticate(self.user_id)
        allowed = self.client.get("/api/vault/quotas")
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_api_me_not_swallowed_by_spa_fallback(self) -> None:
        """Seam 3: GET /api/* unaffected — JSON, not the SPA shell."""
        self._authenticate()
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertEqual(payload["username"], "owner")
        self.assertNotIn("spa-shell", response.text)

    def test_unknown_route_falls_back_to_spa(self) -> None:
        response = self.client.get("/some/unknown/route")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("spa-shell", response.text)

    def test_spa_cache_headers_for_assets_and_index(self) -> None:
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200, index.text)
        self.assertEqual(index.headers.get("cache-control"), "no-store")

        asset = self.client.get("/assets/index-abc123.js")
        self.assertEqual(asset.status_code, 200, asset.text)
        self.assertEqual(
            asset.headers.get("cache-control"),
            "public, max-age=31536000, immutable",
        )

    def test_spa_asset_rejects_path_traversal(self) -> None:
        outside = self.dist_dir / "secret.txt"
        outside.write_text("do-not-leak\n", encoding="utf-8")
        for url in (
            "/assets/%2e%2e/secret.txt",
            "/assets/..%2Fsecret.txt",
            "/assets/foo/%2e%2e/%2e%2e/secret.txt",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404, f"{url}: {response.text}")
            self.assertNotIn("do-not-leak", response.text)

    def test_missing_dist_returns_diagnosable_error(self) -> None:
        missing = Path(self._tmp.name) / "missing-dist"
        object.__setattr__(self.test_settings, "frontend_dist_dir", str(missing))
        response = self.client.get("/")
        self.assertEqual(response.status_code, 503, response.text)
        detail = response.json()["detail"]
        self.assertNotIn("FRONTEND_SPA", detail)
        self.assertIn("npm run build", detail)
        self.assertIn("frontend/dist", detail)

    def test_no_frontend_spa_references_in_sources(self) -> None:
        """Seam 4: no reference to FRONTEND_SPA remains in the sources."""
        hits: list[str] = []
        for path in _iter_source_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "FRONTEND_SPA" in text or "frontend_spa" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(hits, [], f"FRONTEND_SPA still referenced in: {hits}")

    def test_no_deleted_jinja_or_static_paths_in_sources(self) -> None:
        """Seam 5: no reference to deleted templates or static files remains."""
        hits: list[str] = []
        for path in _iter_source_files():
            # This test file names the deleted paths on purpose.
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for fragment in _DELETED_PATH_FRAGMENTS:
                if fragment in text:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{fragment}")
        self.assertEqual(hits, [], f"Deleted paths still referenced in: {hits}")


if __name__ == "__main__":
    unittest.main()
