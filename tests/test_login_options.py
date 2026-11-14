from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


class LoginOptionsTests(unittest.TestCase):
    """The login page learns the OIDC button label without a Session (#650)."""

    def _options(self, **overrides) -> dict:
        with patch("app.main.settings", replace(settings, **overrides)):
            response = TestClient(app).get("/api/login/options")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def test_exposes_configured_provider_name(self) -> None:
        self.assertEqual(
            self._options(oidc_enabled=True, oidc_provider_name="Pocket ID"),
            {"oidc_enabled": True, "oidc_provider_name": "Pocket ID"},
        )

    def test_empty_provider_name_is_null(self) -> None:
        self.assertEqual(
            self._options(oidc_enabled=True, oidc_provider_name=""),
            {"oidc_enabled": True, "oidc_provider_name": None},
        )

    def test_reports_oidc_disabled(self) -> None:
        self.assertFalse(self._options(oidc_enabled=False)["oidc_enabled"])


if __name__ == "__main__":
    unittest.main()
