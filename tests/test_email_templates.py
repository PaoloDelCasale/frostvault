"""SMTP email template rendering (issue #16)."""
from __future__ import annotations

import unittest

from app.services.email_templates import render_email


class EmailTemplateTests(unittest.TestCase):
    def test_known_event_uses_ready_made_template(self) -> None:
        message = render_email(
            "vault_membership_changed",
            vault_id=7,
            reason="coverage while owner is away",
        )
        self.assertEqual(message["subject"], "[FrostVault] Vault membership changed")
        self.assertIn("vault 7", message["body"])
        self.assertIn("coverage while owner is away", message["body"])

    def test_unknown_event_falls_back_to_default_template(self) -> None:
        message = render_email(
            "custom_event",
            title="Custom notice",
            body="Something happened",
        )
        self.assertEqual(message["subject"], "[FrostVault] Custom notice")
        self.assertIn("Something happened", message["body"])


if __name__ == "__main__":
    unittest.main()
