"""Unit tests for per-vault crypt secret handling (issue #6)."""
from __future__ import annotations

import logging
import unittest

from cryptography.fernet import Fernet, InvalidToken

from app.services import vault_crypto


class VaultCryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master_key = Fernet.generate_key().decode("ascii")

    def test_generated_crypt_secrets_are_independent(self) -> None:
        first = vault_crypto.generate_crypt_secrets()
        second = vault_crypto.generate_crypt_secrets()
        self.assertNotEqual(first.password, second.password)
        self.assertNotEqual(first.password2, second.password2)
        self.assertGreaterEqual(len(first.password), 32)
        self.assertGreaterEqual(len(first.password2), 32)

    def test_envelope_encryption_hides_plaintext_from_storage_blob(self) -> None:
        secrets = vault_crypto.generate_crypt_secrets()
        stored = vault_crypto.encrypt_vault_secrets(secrets, self.master_key)

        self.assertNotIn(secrets.password, stored.password_ciphertext)
        self.assertNotIn(secrets.password2, stored.password_ciphertext)
        self.assertNotIn(secrets.password, stored.password2_ciphertext)
        self.assertNotIn(secrets.password2, stored.password2_ciphertext)

        restored = vault_crypto.decrypt_vault_secrets(stored, self.master_key)
        self.assertEqual(restored.password, secrets.password)
        self.assertEqual(restored.password2, secrets.password2)

    def test_wrong_master_key_cannot_reveal_vault_secrets(self) -> None:
        secrets = vault_crypto.generate_crypt_secrets()
        stored = vault_crypto.encrypt_vault_secrets(secrets, self.master_key)
        other_key = Fernet.generate_key().decode("ascii")

        with self.assertRaises(InvalidToken):
            vault_crypto.decrypt_vault_secrets(stored, other_key)

    def test_redact_secrets_removes_plaintext_from_text_and_mappings(self) -> None:
        secrets = vault_crypto.CryptSecrets(
            password="super-secret-password-value",
            password2="super-secret-salt-value",
        )
        redacted = vault_crypto.redact_secrets(
            {
                "message": "failed with super-secret-password-value",
                "nested": ["super-secret-salt-value", {"x": "ok"}],
            },
            secrets,
        )
        self.assertEqual(
            redacted,
            {
                "message": "failed with [REDACTED]",
                "nested": ["[REDACTED]", {"x": "ok"}],
            },
        )
        self.assertEqual(
            vault_crypto.redact_secrets(
                "super-secret-password-value and super-secret-salt-value",
                secrets,
            ),
            "[REDACTED] and [REDACTED]",
        )

    def test_audit_helper_never_emits_plaintext_secrets(self) -> None:
        secrets = vault_crypto.CryptSecrets(
            password="audit-secret-password",
            password2="audit-secret-salt",
        )
        with self.assertLogs("app.audit", level="WARNING") as captured:
            vault_crypto.audit_without_secrets(
                "vault_recovery_exported",
                secrets,
                vault_id=7,
                note="audit-secret-password leaked?",
            )
        joined = "\n".join(captured.output)
        self.assertNotIn("audit-secret-password", joined)
        self.assertNotIn("audit-secret-salt", joined)
        self.assertIn("vault_recovery_exported", joined)


if __name__ == "__main__":
    unittest.main()
