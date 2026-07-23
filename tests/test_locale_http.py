from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.i18n import LOCALE_COOKIE_NAME
from app.main import app
from app.security import hash_password
from tests.test_database import run_alembic


class LocaleHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.username = "alice"
        self.password = "correct horse battery"
        with SQLiteConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, 'Alice', %s, TRUE)
                """,
                (self.username, hash_password(self.password)),
            )

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _login(self) -> None:
        response = self.client.post(
            "/api/login",
            json={"username": self.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)

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
        self._login()
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
        self._login()
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

    def test_login_page_uses_locale_cookie_for_html_lang(self) -> None:
        self.client.cookies.set(LOCALE_COOKIE_NAME, "it")
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('lang="it"', response.text)
        self.assertIn("Accedi", response.text)


if __name__ == "__main__":
    unittest.main()
