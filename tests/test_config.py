from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import fields, replace

from app.config import Settings, validate_settings
from tests.fixtures import override_settings


def _base_settings(**overrides: object) -> Settings:
    base = replace(
        Settings(),
        bootstrap_admin_username="",
        bootstrap_admin_password="",
    )
    return replace(base, **overrides)


def _installed(settings: Settings):
    """Every field of ``settings`` as the application's Settings, for the block."""
    return override_settings(**{field.name: getattr(settings, field.name) for field in fields(Settings)})


class OidcConfigValidationTests(unittest.TestCase):
    def _validate(self, settings: Settings) -> None:
        with _installed(settings):
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
        with _installed(settings):
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
        with _installed(settings):
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


class OperationConcurrencyBoundsTests(unittest.TestCase):
    """The cap rises from 16 to 32 (#701); the value is read at import."""

    def _imported(self, **env: str) -> int:
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"OPERATION_CONCURRENCY", "UPLOAD_CONCURRENCY"}}
        environment.update(env)
        result = subprocess.run(
            [sys.executable, "-c",
             "from app.config import settings; print(settings.operation_concurrency)"],
            env=environment, capture_output=True, text=True, check=True, timeout=60,
        )
        return int(result.stdout.strip())

    def test_operation_concurrency_is_clamped_to_1_through_32(self) -> None:
        for value, expected in (("0", 1), ("1", 1), ("16", 16), ("24", 24),
                                ("32", 32), ("64", 32)):
            with self.subTest(value=value):
                self.assertEqual(self._imported(OPERATION_CONCURRENCY=value), expected)

    def test_upload_concurrency_fallback_shares_the_cap(self) -> None:
        self.assertEqual(self._imported(UPLOAD_CONCURRENCY="40"), 32)


if __name__ == "__main__":
    unittest.main()
