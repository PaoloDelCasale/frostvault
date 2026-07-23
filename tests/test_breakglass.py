from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from app import breakglass
from app.config import settings


class BreakGlassNetworkTests(unittest.TestCase):
    def _with_cidrs(self, raw: str) -> None:
        patched = replace(settings, break_glass_allowed_cidrs=raw)
        patcher = patch("app.breakglass.settings", patched)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loopback_is_allowed_when_no_cidrs_configured(self) -> None:
        self._with_cidrs("")
        self.assertTrue(breakglass.is_break_glass_allowed("127.0.0.1"))
        self.assertTrue(breakglass.is_break_glass_allowed("::1"))

    def test_public_ip_is_denied_when_no_cidrs_configured(self) -> None:
        self._with_cidrs("")
        self.assertFalse(breakglass.is_break_glass_allowed("203.0.113.5"))

    def test_ip_inside_a_configured_cidr_is_allowed(self) -> None:
        self._with_cidrs("10.0.0.0/24, 192.168.1.0/24")
        self.assertTrue(breakglass.is_break_glass_allowed("192.168.1.42"))

    def test_ip_outside_configured_cidrs_is_denied(self) -> None:
        self._with_cidrs("10.0.0.0/24")
        self.assertFalse(breakglass.is_break_glass_allowed("10.0.1.1"))

    def test_missing_or_unparseable_ip_is_denied(self) -> None:
        self._with_cidrs("10.0.0.0/24")
        self.assertFalse(breakglass.is_break_glass_allowed(None))
        self.assertFalse(breakglass.is_break_glass_allowed("not-an-ip"))

    def test_loopback_stays_allowed_even_when_cidrs_are_set(self) -> None:
        self._with_cidrs("10.0.0.0/24")
        self.assertTrue(breakglass.is_break_glass_allowed("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
