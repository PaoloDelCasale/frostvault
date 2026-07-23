"""Envelope encryption and redaction for per-vault crypt secrets (issue #6)."""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..audit import audit_log
from ..config import settings


# Hard-coded key used by `rclone obscure` / `rclone reveal` (AES-256-CTR).
# Keeping a compatible implementation avoids putting vault passwords on argv.
_RCLONE_OBSCURE_KEY = bytes(
    [
        0x9C,
        0x93,
        0x5B,
        0x48,
        0x73,
        0x0A,
        0x55,
        0x4D,
        0x6B,
        0xFD,
        0x7C,
        0x63,
        0xC8,
        0x86,
        0xA9,
        0x2B,
        0xD3,
        0x90,
        0x19,
        0x8E,
        0xB8,
        0x12,
        0x8A,
        0xFB,
        0xF4,
        0xDE,
        0x16,
        0x2B,
        0x8B,
        0x95,
        0xF6,
        0x38,
    ]
)

REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class CryptSecrets:
    password: str
    password2: str


@dataclass(frozen=True)
class StoredCryptSecrets:
    password_ciphertext: str
    password2_ciphertext: str


class MasterKeyError(RuntimeError):
    """Raised when the application master key is missing or invalid."""


def generate_crypt_secrets() -> CryptSecrets:
    """Return two independent high-entropy secrets for an Rclone crypt remote."""
    return CryptSecrets(
        password=secrets.token_urlsafe(32),
        password2=secrets.token_urlsafe(32),
    )


def _fernet(master_key: str) -> Fernet:
    key = master_key.strip()
    if not key:
        raise MasterKeyError("ARCHIVE_MASTER_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MasterKeyError(
            "ARCHIVE_MASTER_KEY must be a url-safe base64-encoded 32-byte key"
        ) from exc


def encrypt_vault_secrets(
    secrets_value: CryptSecrets, master_key: str | None = None
) -> StoredCryptSecrets:
    fernet = _fernet(master_key if master_key is not None else settings.archive_master_key)
    return StoredCryptSecrets(
        password_ciphertext=fernet.encrypt(secrets_value.password.encode("utf-8")).decode(
            "ascii"
        ),
        password2_ciphertext=fernet.encrypt(
            secrets_value.password2.encode("utf-8")
        ).decode("ascii"),
    )


def decrypt_vault_secrets(
    stored: StoredCryptSecrets, master_key: str | None = None
) -> CryptSecrets:
    fernet = _fernet(master_key if master_key is not None else settings.archive_master_key)
    try:
        return CryptSecrets(
            password=fernet.decrypt(stored.password_ciphertext.encode("ascii")).decode(
                "utf-8"
            ),
            password2=fernet.decrypt(
                stored.password2_ciphertext.encode("ascii")
            ).decode("utf-8"),
        )
    except InvalidToken:
        raise


def obscure_for_rclone(plaintext: str) -> str:
    """Obscure a password the way Rclone's config parser expects."""
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(_RCLONE_OBSCURE_KEY), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext.encode("utf-8")) + encryptor.finalize()
    return base64.urlsafe_b64encode(iv + ciphertext).decode("ascii").rstrip("=")


def redact_secrets(value: Any, secrets_value: CryptSecrets | None) -> Any:
    """Recursively replace known vault secrets with ``[REDACTED]``."""
    if secrets_value is None:
        return value
    needles = [secrets_value.password, secrets_value.password2]
    if isinstance(value, str):
        redacted = value
        for needle in needles:
            if needle and needle in redacted:
                redacted = redacted.replace(needle, REDACTED)
        return redacted
    if isinstance(value, dict):
        return {
            key: redact_secrets(item, secrets_value) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item, secrets_value) for item in value]
    return value


def audit_without_secrets(
    event: str,
    secrets_value: CryptSecrets | None,
    *,
    connection: Any | None = None,
    **fields: Any,
) -> None:
    """Emit an audit event after stripping any known vault secrets."""
    audit_log(
        event,
        connection=connection,
        **redact_secrets(fields, secrets_value),
    )


def safe_error_message(exc: BaseException, secrets_value: CryptSecrets | None) -> str:
    return str(redact_secrets(str(exc), secrets_value))
