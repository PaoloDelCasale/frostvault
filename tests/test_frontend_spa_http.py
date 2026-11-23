"""SPA is the only frontend (issue #71 cut-over).

No FRONTEND_SPA flag: HTML routes always serve frontend/dist; API routes stay
JSON.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.fixtures import FrostVaultTestCase, override_settings, seed_vault

REPO_ROOT = Path(__file__).resolve().parents[1]

class FrontendSpaHttpTests(FrostVaultTestCase):
    PASSWORD = "correct horse battery staple"
    owner = {"password": PASSWORD}

    def setUp(self) -> None:
        super().setUp()
        self.dist_dir = self.tmp_path / "dist"
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

        with self.database.connect() as connection:
            self.vault_id = seed_vault(
                connection, name="Docs Archive", source_root="/source"
            )["id"]

        self.enterContext(override_settings(frontend_dist_dir=str(self.dist_dir)))
        self.client = self.http_client()

    def test_root_serves_spa_with_no_flag(self) -> None:
        """Seam 1: GET / returns the SPA with no flag set anywhere."""
        self.assertFalse(hasattr(self.settings, "frontend_spa"))
        self.assertNotIn("FRONTEND_SPA", os.environ)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="root"', response.text)
        self.assertIn("spa-shell", response.text)
        self.assertNotIn("data-can-operate=", response.text)
        self.assertNotIn("/static/style.css", response.text)

    def test_html_routes_serve_spa(self) -> None:
        """Seam 2: every client route serves the SPA, an unknown one included."""
        for path in ("/login", "/vaults/new", "/general", "/problems", "/admin", "/vault/access"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"{path}: {response.text}")
            self.assertIn("spa-shell", response.text, path)
            self.assertIn('id="root"', response.text, path)

    def test_authentication_still_enforced_on_api(self) -> None:
        """Seam 2: the API still requires a session; the SPA does not gate it."""
        denied = self.client.get(f"/api/vaults/{self.vault_id}/operation-policy")
        self.assertEqual(denied.status_code, 401, denied.text)

        self.sign_in()
        allowed = self.client.get(f"/api/vaults/{self.vault_id}/operation-policy")
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_api_me_not_swallowed_by_spa_fallback(self) -> None:
        """Seam 3: GET /api/* unaffected — JSON, not the SPA shell."""
        self.sign_in()
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertEqual(payload["display_name"], "Owner")
        self.assertNotIn("spa-shell", response.text)

    def test_catalog_is_network_only_with_ordinary_session_authentication(self) -> None:
        denied = self.client.get("/api/files")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["cache-control"], "no-store")
        self.sign_in()
        me = self.client.get("/api/me")
        self.assertNotIn("offline_cache_generation", me.json())
        self.assertNotIn("transition_version", me.json())
        self.assertEqual(me.headers["cache-control"], "no-store")
        listing = self.client.get("/api/files", headers={
            "X-FrostVault-Offline-Cache-Authorization": "obsolete-client-value",
        })
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.headers["cache-control"], "no-store")
        self.assertNotIn("X-FrostVault-Offline-Cache-Authorization", listing.headers)

    def test_unknown_route_falls_back_to_spa(self) -> None:
        response = self.client.get("/some/unknown/route")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("spa-shell", response.text)

    def test_unknown_api_route_is_not_found_for_every_method(self) -> None:
        self.sign_in()
        csrf = self.client.get("/api/me").json()["csrf_token"]
        for method in ("get", "post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = self.client.request(
                    method.upper(),
                    "/api/no-such-route",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 404, response.text)

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
        missing = self.tmp_path / "missing-dist"
        self.enterContext(override_settings(frontend_dist_dir=str(missing)))
        response = self.client.get("/")
        self.assertEqual(response.status_code, 503, response.text)
        detail = response.json()["detail"]
        self.assertNotIn("FRONTEND_SPA", detail)
        self.assertIn("npm run build", detail)
        self.assertIn("frontend/dist", detail)

if __name__ == "__main__":
    unittest.main()
