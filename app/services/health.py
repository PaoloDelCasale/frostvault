"""Process liveness helpers and dependency-aware readiness (issues #16, #201)."""
from __future__ import annotations

import threading
import time
from typing import Any

from cryptography.fernet import Fernet

from ..config import settings
from ..database import db
from . import source_layout
from .vault_crypto import MasterKeyError, StoredCryptSecrets, decrypt_vault_secrets

_lock = threading.Lock()
_worker_heartbeat_at: float | None = None
# Workers are considered stale after this many seconds without a heartbeat.
WORKER_STALE_SECONDS = 120.0

CRYPT_CUSTODY_READY = "ready"
CRYPT_CUSTODY_NOT_REQUIRED = "not_required"
CRYPT_CUSTODY_MISSING_MASTER_KEY = "missing_master_key"
CRYPT_CUSTODY_INVALID_MASTER_KEY = "invalid_master_key"
CRYPT_CUSTODY_UNDECRYPTABLE = "undecryptable"
CRYPT_CUSTODY_UNAVAILABLE = "unavailable"


def mark_worker_heartbeat(now: float | None = None) -> None:
    """Record that the background worker loop is alive."""
    global _worker_heartbeat_at
    with _lock:
        _worker_heartbeat_at = now if now is not None else time.monotonic()


def worker_is_healthy(
    *,
    now: float | None = None,
    stale_after: float = WORKER_STALE_SECONDS,
) -> bool:
    with _lock:
        heartbeat = _worker_heartbeat_at
    if heartbeat is None:
        return False
    current = now if now is not None else time.monotonic()
    return (current - heartbeat) <= stale_after


def check_database() -> bool:
    """Return True when a trivial database round-trip succeeds."""
    try:
        with db() as connection:
            row = connection.execute("SELECT 1 AS ok").fetchone()
            return bool(row and (row.get("ok") == 1 or list(row.values())[0] == 1))
    except Exception:
        return False


def check_config() -> bool:
    """Return True when core settings look startable."""
    try:
        if settings.db_backend not in {"sqlite", "postgresql"}:
            return False
        if settings.db_backend == "sqlite" and not str(settings.sqlite_path).strip():
            return False
        return True
    except Exception:
        return False


def _validated_archive_master_key() -> tuple[str | None, str | None]:
    """Return a usable deployment key or its non-secret readiness state."""
    candidate = getattr(settings, "archive_master_key", "")
    if not isinstance(candidate, str):
        return None, CRYPT_CUSTODY_INVALID_MASTER_KEY
    key = candidate.strip()
    if not key:
        return None, CRYPT_CUSTODY_MISSING_MASTER_KEY
    try:
        Fernet(key.encode("ascii"))
    except (TypeError, UnicodeError, ValueError):
        return None, CRYPT_CUSTODY_INVALID_MASTER_KEY
    return key, None


def crypt_custody_state() -> str:
    """Return a non-secret state for every persisted crypt Vault's custody.

    A syntactically valid key is not sufficient: every crypt row must decrypt
    before readiness can claim crypt operations are possible. Exceptions remain
    intentionally collapsed into fixed states so neither master-key material nor
    ciphertext is exposed through the health endpoint or logs.
    """
    try:
        with db() as connection:
            crypt_rows = connection.execute(
                """
                SELECT crypt_password_ciphertext, crypt_password2_ciphertext
                FROM vaults
                WHERE encryption_mode = %s
                """,
                ("crypt",),
            ).fetchall()
    except Exception:
        return CRYPT_CUSTODY_UNAVAILABLE

    if not crypt_rows:
        return CRYPT_CUSTODY_NOT_REQUIRED

    master_key, invalid_state = _validated_archive_master_key()
    if invalid_state is not None:
        return invalid_state
    assert master_key is not None

    try:
        for vault in crypt_rows:
            decrypt_vault_secrets(
                StoredCryptSecrets(
                    password_ciphertext=vault["crypt_password_ciphertext"],
                    password2_ciphertext=vault["crypt_password2_ciphertext"],
                ),
                master_key,
            )
    except MasterKeyError:
        # Validation above ordinarily prevents this path, but remain fail-closed
        # if the crypto implementation rejects a key differently in the future.
        return CRYPT_CUSTODY_INVALID_MASTER_KEY
    except Exception:
        return CRYPT_CUSTODY_UNDECRYPTABLE
    return CRYPT_CUSTODY_READY


def crypt_custody_is_ready(state: str) -> bool:
    """Whether a non-secret custody state permits the process to become ready."""
    return state in {CRYPT_CUSTODY_NOT_REQUIRED, CRYPT_CUSTODY_READY}


def readiness_report() -> dict[str, Any]:
    database_ready = check_database()
    crypt_custody = (
        crypt_custody_state() if database_ready else CRYPT_CUSTODY_UNAVAILABLE
    )
    checks = {
        "database": database_ready,
        "worker": worker_is_healthy(),
        "config": check_config(),
        "sources_layout": source_layout.sources_layout_is_ready(),
        "crypt_custody": crypt_custody_is_ready(crypt_custody),
    }
    ready = all(checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "crypt_custody": crypt_custody,
    }
