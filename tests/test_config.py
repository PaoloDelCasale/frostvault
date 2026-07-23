from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from app import config
from app.config import Settings, validate_settings


def _base_settings(**overrides: object) -> Settings:
    base = replace(
        Settings(),
        bootstrap_admin_username="",
        bootstrap_admin_password="",
    )
    return replace(base, **overrides)


class OidcConfigValidationTests(unittest.TestCase):
    def _validate(self, settings: Settings) -> None:
        with patch.object(config, "settings", settings):
            validate_settings()

    def test_enabled_oidc_without_client_secret_is_rejected(self) -> None:
        settings = _base_settings(
            oidc_enabled=True,
            oidc_issuer="https://issuer.example",
            oidc_client_id="client",
            oidc_client_secret="",
        )
        with self.assertRaises(RuntimeError) as error:
            self._validate(settings)
        self.assertIn("OIDC", str(error.exception))

    def test_enabled_oidc_without_issuer_is_rejected(self) -> None:
        settings = _base_settings(
            oidc_enabled=True,
            oidc_issuer="",
            oidc_client_id="client",
            oidc_client_secret="secret",
        )
        with self.assertRaises(RuntimeError):
            self._validate(settings)

    def test_fully_configured_oidc_is_accepted(self) -> None:
        settings = _base_settings(
            oidc_enabled=True,
            oidc_issuer="https://issuer.example",
            oidc_client_id="client",
            oidc_client_secret="secret",
        )
        self._validate(settings)

    def test_disabled_oidc_needs_no_oidc_settings(self) -> None:
        settings = _base_settings(oidc_enabled=False)
        self._validate(settings)


class BreakGlassConfigValidationTests(unittest.TestCase):
    def _validate(self, settings: Settings) -> None:
        with patch.object(config, "settings", settings):
            validate_settings()

    def test_invalid_cidr_entry_is_rejected(self) -> None:
        settings = _base_settings(break_glass_allowed_cidrs="10.0.0.0/24, nonsense")
        with self.assertRaises(RuntimeError) as error:
            self._validate(settings)
        self.assertIn("BREAK_GLASS_ALLOWED_CIDRS", str(error.exception))

    def test_valid_cidr_list_is_accepted(self) -> None:
        settings = _base_settings(
            break_glass_allowed_cidrs="10.0.0.0/24, 2001:db8::/32"
        )
        self._validate(settings)

    def test_empty_cidr_list_is_accepted(self) -> None:
        settings = _base_settings(break_glass_allowed_cidrs="")
        self._validate(settings)


class ProductionHardeningConfigTests(unittest.TestCase):
    def _validate(self, settings: Settings) -> None:
        with patch.object(config, "settings", settings):
            validate_settings()

    def test_secure_cookie_requires_allowed_hosts(self) -> None:
        settings = _base_settings(
            cookie_secure=True,
            allowed_hosts="",
            trusted_proxies="10.0.0.0/8",
        )
        with self.assertRaises(RuntimeError) as error:
            self._validate(settings)
        self.assertIn("ALLOWED_HOSTS", str(error.exception))

    def test_secure_cookie_requires_trusted_proxies(self) -> None:
        settings = _base_settings(
            cookie_secure=True,
            allowed_hosts="archive.example",
            trusted_proxies="",
        )
        with self.assertRaises(RuntimeError) as error:
            self._validate(settings)
        self.assertIn("TRUSTED_PROXIES", str(error.exception))

    def test_secure_cookie_with_both_present_is_accepted(self) -> None:
        settings = _base_settings(
            cookie_secure=True,
            allowed_hosts="archive.example",
            trusted_proxies="10.0.0.0/8",
        )
        self._validate(settings)

    def test_invalid_trusted_proxy_cidr_is_rejected(self) -> None:
        settings = _base_settings(trusted_proxies="10.0.0.0/8, nonsense")
        with self.assertRaises(RuntimeError) as error:
            self._validate(settings)
        self.assertIn("TRUSTED_PROXIES", str(error.exception))

    def test_development_defaults_need_no_hardening_env(self) -> None:
        # Without a secure cookie the app may run locally with no proxy in front.
        self._validate(_base_settings(cookie_secure=False))


if __name__ == "__main__":
    unittest.main()
