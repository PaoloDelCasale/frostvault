"""What the clusters of the catalog share (#803).

The catalog is divided along its clusters, each in a Module of its own
(``archive_versions``, ``file_listing``, ``file_renames``, ``local_copies``,
``job_admission``). This Module holds what more than one of them needs, so that none has to
reach into another: the boundary conversions of timestamps, digests and ids, the
error of a Vault File that is not there, and the revocation of pending
Delayed Cloud Deletions that a reappearance and a rename share. The Vault lock
an identity creation takes first is ``vault_locks.vault_lock_touch``.

It imports no cluster and no other service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence


class VaultFileNotFound(LookupError):
    """A Vault File could not be resolved in the expected Vault."""


# A Job holds its File until it ends. A pending Delayed Cloud Deletion is only
# the Owner's consent: a reappearance or a rename revokes it (#587, #594).
DELETION_CONSENT_SQL = "action='cloud-purge' AND status='pending_delay'"


def as_timestamp(value: Any) -> datetime | None:
    """Convert a boundary timestamp (ISO text or datetime) to an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def required_timestamp(value: Any) -> datetime:
    parsed = as_timestamp(value)
    if parsed is None:
        raise ValueError("A timestamp is required")
    return parsed


def sha256_digest(value: str | bytes) -> bytes:
    """A SHA-256 digest from hex text or raw bytes, validated once here."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    else:
        text = str(value)
        if len(text) != 64:
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        raw = bytes.fromhex(text)
    if len(raw) != 32:
        raise ValueError("SHA-256 must be 32 bytes")
    return raw


def digest_hex(value: Any) -> str | None:
    return None if value is None else bytes(value).hex()


def id_text(value: Any) -> str | None:
    return None if value is None else str(value)


def revoke_deletion_consents(
    connection: Any, vault_id: int, file_ids: Sequence[Any], at: Any, *, reason: str
) -> None:
    """Permanently revoke pending Delayed Cloud Deletions of these files.

    The whole selection is revoked, never left partially authorized: a
    reappearance or rename is new evidence the consent never covered.
    An automatic group (Automatic Duplicate Cleanup, ADR-0048) loses only
    these files: each of its members was chosen on its own evidence.
    Callers already hold the Vault lock.
    """
    ids = [str(file_id) for file_id in file_ids if file_id]
    if not ids:
        return
    stamp = required_timestamp(at)
    connection.execute(
        f"""
            UPDATE jobs
            SET status='cancelled', message=%s, updated_at=%s, finished_at=%s
            WHERE vault_id=%s AND {DELETION_CONSENT_SQL}
              AND (
                  file_id = ANY(%s::uuid[])
                  OR group_id IN (
                      SELECT consent.group_id FROM jobs consent
                      WHERE consent.vault_id=%s
                        AND consent.file_id = ANY(%s::uuid[])
                        AND consent.action='cloud-purge'
                        AND consent.status='pending_delay'
                        AND NOT EXISTS (
                            SELECT 1 FROM cloud_purge_requests request
                            WHERE request.group_id = consent.group_id
                              AND request.automatic
                        )
                  )
              )
            """,
        (reason, stamp, stamp, vault_id, ids, vault_id, ids),
    )
