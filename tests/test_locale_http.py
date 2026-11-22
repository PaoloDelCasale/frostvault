from __future__ import annotations

import unittest

from app.i18n import LOCALE_COOKIE_NAME
from tests.fixtures import FrostVaultTestCase, override_settings
from tests.spa_fixture import write_spa_dist


class LocaleHttpTests(FrostVaultTestCase):
    password = "correct horse battery"
    owner = {"display_name": "Alice", "password": password}

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(override_settings(
            frontend_dist_dir=str(write_spa_dist(self.tmp_path)),
        ))
        self.client = self.http_client()

    def test_catalog_endpoint_returns_english_by_default(self) -> None:
        response = self.client.get("/api/i18n/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["locale"], "en")
        self.assertEqual(payload["messages"]["ui.sign_out"], "Sign out")
        self.assertEqual(payload["locales"], ["en", "it"])

    def test_catalog_endpoint_honors_locale_query(self) -> None:
        response = self.client.get("/api/i18n/catalog", params={"locale": "it"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["locale"], "it")
        self.assertEqual(payload["messages"]["ui.sign_out"], "Esci")

    def test_put_locale_sets_cookie_and_returns_italian_catalog(self) -> None:
        self.sign_in(password=self.password)
        response = self.client.put(
            "/api/locale",
            json={"locale": "it"},
            headers={"X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["locale"], "it")
        self.assertEqual(payload["message"], "Lingua aggiornata")
        self.assertEqual(self.client.cookies.get(LOCALE_COOKIE_NAME), "it")

        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["locale"], "it")

    def test_put_locale_cookie_value_is_only_allowlisted_constant(self) -> None:
        """Seam: PUT /api/locale — Set-Cookie never echoes raw client locale text."""
        self.sign_in(password=self.password)
        response = self.client.put(
            "/api/locale",
            json={"locale": "it-IT"},
            headers={"X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""},
        )
        self.assertEqual(response.status_code, 200, response.text)
        set_cookie = response.headers.get("set-cookie", "")
        self.assertRegex(
            set_cookie,
            rf"(?i){LOCALE_COOKIE_NAME}=it(;|,|$)",
        )
        self.assertNotIn("it-IT", set_cookie)

    def test_login_page_serves_spa_shell(self) -> None:
        """HTML shell is the SPA; locale copy comes from /api/i18n/catalog."""
        self.client.cookies.set(LOCALE_COOKIE_NAME, "it")
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="root"', response.text)

        catalog = self.client.get("/api/i18n/catalog", params={"locale": "it"})
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.json()["messages"]["login.submit"], "Accedi")


if __name__ == "__main__":
    unittest.main()
