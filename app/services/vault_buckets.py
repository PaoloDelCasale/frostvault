"""The dedicated Vault Bucket FrostVault creates for every new Vault (#735).

ADR-0049. A new Vault gets ``frostvault-<slug>-<8 random [a-z0-9]>`` in
``AWS_DEFAULT_REGION``: the slug is the Vault name in lowercase ASCII, at most
40 characters, and never starts with ``system`` (the System Bucket is
``frostvault-system-<8>``: such a slug becomes ``vault-system...``). Its objects live at the bucket root (the target's
``key_layout`` is ``bucket_root``, so its prefix is empty). The bucket gets the
configuration every FrostVault bucket has (``bucket_provisioning``: Block
Public Access, versioning, SSE-S3, ``BucketOwnerEnforced``), an unfiltered
lifecycle that is exactly the Vault's Storage Plan plus the incomplete
multipart abort, with ``TransitionDefaultMinimumObjectSize`` 128 KiB, and the
tags ``frostvault:vault=<uuid>`` and ``frostvault:name=<name>``. An optional
Governance Retention Lock is chosen when the Vault is created (#736).

Creation is two steps. ``reserve`` saves the generated name as the Vault's
target and the Vault as ``provisioning``, before any S3 request. ``provision``
then creates, configures and verifies the bucket. A passing verification
records ``verified_at``; the Vault becomes ``active`` 15 minutes later
(``VERSIONING_SETTLE_SECONDS``, the wait AWS recommends after enabling
versioning before writing objects), from the background loop. Both are idempotent: an interrupted creation
resumes on the same name (``BucketAlreadyOwnedByYou`` means already created)
from the request that started it, a retry by the Owner, or the background
loop after a restart (``resume_all``). A name owned by another AWS account is
replaced (at most three in a row), never probed. While a creation fails, the
Vault stays provisioning, keeps its last error and has an open Problem: its
bucket may already exist without being in use. An unexpected error is recorded
by its type only, never its message. The Owner may cancel the creation
(``cancel_creation``) while the Vault is provisioning: the Vault leaves the
catalog and a bucket already created stays in AWS, named in the outcome and
the audit record (FrostVault has no ``s3:DeleteBucket``).

The lifecycle is written whole (``bucket_provisioning.apply_lifecycle``) only
by ``provision`` and by ``sync_lifecycle`` when it differs from the Storage
Plan; ``sync_lifecycle`` never touches the rest of the configuration.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import BotoCoreError, ClientError

from . import bucket_provisioning as buckets
from .lifecycle_profiles import LifecycleProfile, profile_from_json
from .s3_lifecycle_rules import vault_bucket_lifecycle_rules

KEY_LAYOUT = "bucket_root"
SLUG_MAX_LENGTH = 40
# The System Bucket is frostvault-system-<8>: no Vault Bucket name starts
# with frostvault-system-, so a slug "system" or "system-..." gets this prefix.
_RESERVED_SLUG = "system"
_RESERVED_SLUG_PREFIX = "vault-"
_NAME_ATTEMPTS = 3
PROBLEM_EVENT = "vault_bucket_failed"
# While a Vault Bucket is not ready the background loop retries this often.
RETRY_SECONDS = 15 * 60
# AWS recommends waiting 15 minutes after enabling versioning before writing
# objects: a verified Vault Bucket becomes active this long after verification.
VERSIONING_SETTLE_SECONDS = 15 * 60
_logger = logging.getLogger(__name__)
# Characters AWS accepts in a tag value besides letters, digits and spaces.
_TAG_PUNCTUATION = frozenset("+-=._:/@")
TAG_VAULT = "frostvault:vault"
TAG_NAME = "frostvault:name"

_locks_guard = threading.Lock()
_locks: dict[int, threading.Lock] = {}


class VaultBucketUnavailable(RuntimeError):
    """Vault Buckets cannot be created on this server (region or credentials)."""


class CancelRefused(RuntimeError):
    """The creation cannot be cancelled: ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def slug(name: str) -> str:
    """The Vault name as a bucket-name fragment: lowercase ASCII, at most 40.

    Accents are dropped, every other run of characters becomes one hyphen; a
    name with no ASCII letter or digit becomes ``vault``. A slug that is
    ``system`` or starts with ``system-`` becomes ``vault-system...``: the
    System Bucket alone is ``frostvault-system-*``.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    parts, current = [], []
    for character in ascii_name.lower():
        if character.isascii() and character.isalnum():
            current.append(character)
        elif current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    value = "-".join(parts)[:SLUG_MAX_LENGTH].strip("-") or "vault"
    if value == _RESERVED_SLUG or value.startswith(f"{_RESERVED_SLUG}-"):
        value = f"{_RESERVED_SLUG_PREFIX}{value}"[:SLUG_MAX_LENGTH].strip("-")
    return value


def new_bucket_name(vault_name: str) -> str:
    return buckets.random_bucket_name(f"{buckets.NAME_PREFIX}{slug(vault_name)}")


def tag_name(name: str) -> str:
    """The Vault name as an S3 tag value: characters AWS refuses become ``_``."""
    value = "".join(
        character if character.isalnum() or character == " " or character in _TAG_PUNCTUATION
        else "_" for character in name.strip())
    return value[:256] or "_"


def tags(vault_uuid: str, vault_name: str) -> dict[str, str]:
    return {TAG_VAULT: str(vault_uuid), TAG_NAME: tag_name(vault_name)}


def spec(*, bucket: str, region: str, vault_uuid: str, vault_name: str,
         profile: LifecycleProfile, retention_lock_days: int | None) -> buckets.BucketSpec:
    lock = buckets.RetentionLock(days=int(retention_lock_days)) if retention_lock_days else None
    return buckets.BucketSpec(
        name=bucket, region=region, tags=tags(vault_uuid, vault_name),
        lifecycle_rules=vault_bucket_lifecycle_rules(str(vault_uuid), profile),
        retention_lock=lock,
        transition_minimum_object_size=buckets.TRANSITION_MINIMUM_OBJECT_SIZE,
    )


_ROW_SQL = """
    SELECT v.id, v.uuid, v.name, v.lifecycle, v.target_id, v.source_root,
           v.storage_plan::text AS storage_plan_json,
           t.bucket, t.region, t.key_layout, t.retention_lock_days,
           p.stage, p.attempts, p.last_attempt_at, p.last_error, p.requested_at,
           p.verified_at
    FROM vaults v JOIN vault_targets t ON t.id = v.target_id
    LEFT JOIN vault_bucket_provisioning p ON p.vault_id = v.id
"""


def _spec_for(row: dict[str, Any]) -> buckets.BucketSpec:
    return spec(bucket=row["bucket"], region=row["region"], vault_uuid=str(row["uuid"]),
                vault_name=row["name"], profile=profile_from_json(row["storage_plan_json"]),
                retention_lock_days=row["retention_lock_days"])


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _active_at(row: dict[str, Any]) -> datetime | None:
    """When a verified Vault Bucket's Vault becomes active."""
    verified = row.get("verified_at")
    if verified is None:
        return None
    if isinstance(verified, str):
        verified = datetime.fromisoformat(verified)
    return verified + timedelta(seconds=VERSIONING_SETTLE_SECONDS)


def _public(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    provisioning = row["lifecycle"] == "provisioning"
    active_at = _active_at(row) if provisioning else None
    if not provisioning:
        state = "ready"
    elif row["last_error"]:
        state = "failed"
    elif active_at is not None:
        # Verified: the Vault waits for versioning to settle (N minutes left).
        state = "settling"
    else:
        state = "creating"
    remaining = None
    if active_at is not None:
        remaining = max(0, math.ceil((active_at - (now or _now())).total_seconds()))
    return {
        "vault_id": int(row["id"]),
        "bucket": row["bucket"],
        "region": row["region"],
        "dedicated": row["key_layout"] == KEY_LAYOUT,
        "state": state,
        "stage": row["stage"] if provisioning else None,
        "attempts": int(row["attempts"] or 0) if provisioning else 0,
        "last_attempt_at": _iso(row["last_attempt_at"]) if provisioning else None,
        "error": row["last_error"] if provisioning else None,
        "retention_lock_days": row["retention_lock_days"],
        "active_at": _iso(active_at),
        "active_in_seconds": remaining,
    }


def status(connection: Any, vault_id: int) -> dict[str, Any] | None:
    row = connection.execute(f"{_ROW_SQL} WHERE v.id=%s", (vault_id,)).fetchone()
    return _public(row) if row else None


def provisioning_states(connection: Any) -> dict[int, dict[str, Any]]:
    """The bucket state of every provisioning Vault, by Vault id."""
    rows = connection.execute(f"{_ROW_SQL} WHERE v.lifecycle='provisioning'").fetchall()
    return {int(row["id"]): _public(row) for row in rows}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def insert_target(connection: Any, *, bucket: str, region: str,
                  retention_lock: buckets.RetentionLock | None) -> int:
    return int(connection.execute(
        """INSERT INTO vault_targets(provider, bucket, region, key_layout, retention_lock_days)
           VALUES ('s3', %s, %s, %s, %s) RETURNING id""",
        (bucket, region, KEY_LAYOUT, retention_lock.days if retention_lock else None),
    ).fetchone()["id"])


def reserve(connection: Any, *, vault_name: str, region: str,
            retention_lock: buckets.RetentionLock | None = None) -> int:
    """Save a new Vault Bucket name as a target, before any S3 request.

    Returns the target id; the caller inserts the ``provisioning`` Vault and
    calls ``start``, all in its own transaction.
    """
    return insert_target(connection, bucket=new_bucket_name(vault_name), region=region,
                         retention_lock=retention_lock)


def start(connection: Any, vault_id: int) -> None:
    connection.execute(
        "INSERT INTO vault_bucket_provisioning(vault_id, requested_at) VALUES (%s, %s)",
        (vault_id, _now()))


def _vault_lock(vault_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(int(vault_id), threading.Lock())


def _default_client(region: str) -> Any:
    from ..storage import s3_client

    return s3_client(region=region)


def client_for_region(region: str, client_for: Callable[[str], Any] | None = None) -> Any:
    """An S3 client for ``region``; refuses without a region or credentials."""
    if not (region or "").strip():
        raise VaultBucketUnavailable("Creating a Vault Bucket needs AWS_DEFAULT_REGION")
    try:
        return (client_for or _default_client)(region)
    except Exception:  # noqa: BLE001 - placeholder credentials, unusable SDK setup
        raise VaultBucketUnavailable(
            "Creating a Vault Bucket needs AWS credentials (AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY, or another SDK credential source)") from None


def _database() -> Callable[[], Any]:
    from ..database import db

    return db


class _Failed(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# The step a provisioning run is in, as SDK failures name it (N6).
_PHASES = {"creation": "the bucket creation", "configuration": "the bucket configuration",
           "verification": "the bucket verification"}


def _sdk_failure(phase: str, exc: BaseException) -> _Failed:
    action = _PHASES[phase]
    if isinstance(exc, ClientError):
        code = buckets.error_code(exc)
        return _Failed(f"S3 refused {action} ({code})")
    # Provider messages can name accounts or endpoints: the type is enough.
    return _Failed(f"S3 could not be reached during {action} ({type(exc).__name__})")


def internal_error(exc: BaseException) -> str:
    """The stored text of an unexpected failure: its type, never its message."""
    return f"internal error ({type(exc).__name__})"


def provision(vault_id: int, *, database: Callable[[], Any] | None = None,
              client: Any = None, client_for: Callable[[str], Any] | None = None,
              blocking: bool = True) -> dict[str, Any] | None:
    """Create, configure and verify the Vault's bucket; activate it when due.

    Returns the bucket state (``status``). A failure is recorded on the Vault,
    with a Problem, and returned, not raised; the Vault stays provisioning.
    A verified bucket makes no S3 request: its Vault becomes active once
    ``VERSIONING_SETTLE_SECONDS`` have passed since the verification. A Vault
    that is not provisioning is returned as it is. With ``blocking=False``
    a creation already running for this Vault returns None at once.
    """
    database = database or _database()
    lock = _vault_lock(vault_id)
    if not lock.acquire(blocking=blocking):
        return None
    try:
        with database() as connection:
            row = connection.execute(f"{_ROW_SQL} WHERE v.id=%s", (vault_id,)).fetchone()
        if row is None:
            raise LookupError("Vault not found")
        if row["lifecycle"] != "provisioning":
            return _public(row)
        if row["stage"] == "verified":
            return _activate_when_due(database, row)
        phase = "creation"
        try:
            if client is None:
                client = client_for_region(row["region"], client_for)
            row = _create(database, client, row)
            phase = "configuration"
            current = _spec_for(row)
            try:
                buckets.configure_bucket(client, current)
            except buckets.BucketProvisioningError as exc:
                raise _Failed(str(exc)) from None
            _set_stage(database, row, "configured")
            phase = "verification"
            verification = buckets.verify_bucket(client, current)
            if not verification.ok:
                raise _Failed("Verification failed: " + verification.failure_message())
        except VaultBucketUnavailable as exc:
            return _record_failure(database, row, str(exc))
        except _Failed as exc:
            return _record_failure(database, row, exc.message)
        except (ClientError, BotoCoreError) as exc:
            return _record_failure(database, row, _sdk_failure(phase, exc).message)
        except Exception as exc:  # noqa: BLE001 - recorded with a Problem, never lost
            _logger.warning("Vault Bucket creation of Vault %s failed: %s",
                            vault_id, type(exc).__name__)
            return _record_failure(database, row, internal_error(exc))
        return _verified(database, row)
    finally:
        lock.release()


def _create(database: Callable[[], Any], client: Any, row: dict[str, Any]) -> dict[str, Any]:
    """CreateBucket on the saved name, replacing a name another account owns."""
    for _ in range(_NAME_ATTEMPTS):
        try:
            buckets.create_bucket(client, _spec_for(row))
        except buckets.BucketNameUnavailable:
            row = _rename(database, row)
            continue
        except buckets.BucketProvisioningError as exc:
            raise _Failed(str(exc)) from None
        _set_stage(database, row, "created")
        return row
    raise _Failed("Every generated bucket name belongs to another AWS account; retry later")


def _rename(database: Callable[[], Any], row: dict[str, Any]) -> dict[str, Any]:
    with database() as connection:
        target_id = insert_target(
            connection, bucket=new_bucket_name(row["name"]), region=row["region"],
            retention_lock=(buckets.RetentionLock(days=int(row["retention_lock_days"]))
                            if row["retention_lock_days"] else None))
        changed = connection.execute(
            """UPDATE vaults SET target_id=%s
               WHERE id=%s AND target_id=%s AND lifecycle='provisioning'""",
            (target_id, row["id"], row["target_id"])).rowcount
        if changed != 1:
            raise _Failed("The Vault's bucket name changed during its creation; retrying")
        connection.execute(
            "UPDATE vault_bucket_provisioning SET stage='reserved' WHERE vault_id=%s",
            (row["id"],))
        return connection.execute(f"{_ROW_SQL} WHERE v.id=%s", (row["id"],)).fetchone()


def _set_stage(database: Callable[[], Any], row: dict[str, Any], stage: str) -> None:
    with database() as connection:
        connection.execute(
            """UPDATE vault_bucket_provisioning p SET stage=%s
               FROM vaults v WHERE v.id=p.vault_id AND p.vault_id=%s
                 AND v.target_id=%s AND v.lifecycle='provisioning'""",
            (stage, row["id"], row["target_id"]))


def _record_failure(database: Callable[[], Any], row: dict[str, Any],
                    message: str) -> dict[str, Any]:
    from .notifications import set_problem

    with database() as connection:
        connection.execute(
            """UPDATE vault_bucket_provisioning
               SET attempts=attempts+1, last_attempt_at=%s, last_error=%s
               WHERE vault_id=%s""", (_now(), message, row["id"]))
        current = connection.execute(f"{_ROW_SQL} WHERE v.id=%s", (row["id"],)).fetchone()
        if current is None or current["lifecycle"] != "provisioning":
            return _public(current) if current else _public(row)
        set_problem(connection, key=f"vault_bucket:{row['id']}", event=PROBLEM_EVENT,
                    vault_id=int(row["id"]), active=True,
                    params={"vault": current["name"], "bucket": current["bucket"],
                            "error": message, "vault_bucket": True})
        return _public(current)


def _verified(database: Callable[[], Any], row: dict[str, Any]) -> dict[str, Any]:
    """Record the passing verification; the Vault waits for versioning to settle."""
    from .notifications import set_problem

    with database() as connection:
        connection.execute(
            """UPDATE vault_bucket_provisioning p
               SET stage='verified', verified_at=%s, last_attempt_at=%s, last_error=NULL
               FROM vaults v WHERE v.id=p.vault_id AND p.vault_id=%s
                 AND v.target_id=%s AND v.lifecycle='provisioning'""",
            (_now(), _now(), row["id"], row["target_id"]))
        # The bucket is right: nothing is left for the Owner to fix.
        set_problem(connection, key=f"vault_bucket:{row['id']}", event=PROBLEM_EVENT,
                    vault_id=int(row["id"]), active=False)
        current = connection.execute(f"{_ROW_SQL} WHERE v.id=%s", (row["id"],)).fetchone()
    if current is None:
        return _public(row)
    return _activate_when_due(database, current)


def _activate_when_due(database: Callable[[], Any], row: dict[str, Any]) -> dict[str, Any]:
    active_at = _active_at(row)
    if active_at is None or active_at > _now():
        return _public(row)
    return _activate(database, row)


def _activate(database: Callable[[], Any], row: dict[str, Any]) -> dict[str, Any]:
    from .audit_events import record_audit_event
    from .notifications import set_problem

    with database() as connection:
        changed = connection.execute(
            """UPDATE vaults SET lifecycle='active'
               WHERE id=%s AND target_id=%s AND lifecycle='provisioning'""",
            (row["id"], row["target_id"])).rowcount
        if changed == 1:
            attempts = connection.execute(
                "DELETE FROM vault_bucket_provisioning WHERE vault_id=%s RETURNING attempts",
                (row["id"],)).fetchone()
            record_audit_event(
                connection, event="vault.bucket_ready", actor="system",
                vault_id=int(row["id"]), outcome="success", vault_uuid=str(row["uuid"]),
                vault_name=row["name"], bucket=row["bucket"], region=row["region"],
                retention_lock_days=row["retention_lock_days"],
                verified_at=_iso(row.get("verified_at")),
                failed_attempts=int(attempts["attempts"]) if attempts else 0)
        set_problem(connection, key=f"vault_bucket:{row['id']}", event=PROBLEM_EVENT,
                    vault_id=int(row["id"]), active=False)
        return status(connection, int(row["id"])) or _public(row)


def _due(row: dict[str, Any], now: datetime) -> bool:
    """Whether the background loop has work for this provisioning Vault now."""
    if row["stage"] == "verified":
        active_at = _active_at(row)
        return active_at is not None and active_at <= now
    last = row["last_attempt_at"]
    return last is None or last + timedelta(seconds=RETRY_SECONDS) <= now


def resume_all(*, database: Callable[[], Any] | None = None,
               client_for: Callable[[str], Any] | None = None,
               on_error: Callable[[int, Exception], None] | None = None,
               ) -> list[dict[str, Any]]:
    """Continue every due Vault Bucket creation (after a restart or a failure).

    A creation never tried (an interrupted request, a restart) or failed at
    least ``RETRY_SECONDS`` ago is retried; a verified one is activated once
    its versioning has settled. Each Vault runs on its own: an error escaping
    one (the catalog unavailable) goes to ``on_error`` and the others still
    run. A creation running right now is skipped. Returns the states reached.
    """
    database = database or _database()
    now = _now()
    with database() as connection:
        rows = connection.execute(
            """SELECT v.id, p.stage, p.last_attempt_at, p.verified_at
               FROM vaults v LEFT JOIN vault_bucket_provisioning p ON p.vault_id = v.id
               WHERE v.lifecycle='provisioning' ORDER BY v.id""").fetchall()
    states = []
    for row in rows:
        if not _due(row, now):
            continue
        vault_id = int(row["id"])
        try:
            state = provision(vault_id, database=database, client_for=client_for, blocking=False)
        except LookupError:
            continue  # Cancelled meanwhile.
        except Exception as exc:  # noqa: BLE001 - one Vault never stops the others
            _logger.warning("Vault Bucket creation of Vault %s could not run: %s",
                            vault_id, type(exc).__name__)
            if on_error is not None:
                on_error(vault_id, exc)
            continue
        if state is not None:
            states.append(state)
    return states


def _bucket_left(row: dict[str, Any]) -> str:
    """Whether the bucket of a cancelled creation exists in AWS.

    ``created``: CreateBucket answered for this name. ``possible``: the name
    was only reserved, but an attempt may have reached AWS before it stopped.
    """
    return "possible" if (row["stage"] or "reserved") == "reserved" else "created"


def cancel_creation(vault_id: int, *, actor: str = "owner",
                    database: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Remove a provisioning Vault from the catalog (the Owner's cancellation).

    Only while the Vault is provisioning and no creation run holds it. The
    Vault row goes with its provisioning state and Problems, which releases
    its root; an empty managed directory is removed, an adopted folder is
    never touched. The reserved target stays (Vault Targets are immutable).
    A bucket already created is not deleted (only a Vault Deletion may, #739):
    the outcome and the audit record name it for the Owner to remove by hand.
    """
    from .audit_events import record_audit_event
    from .source_layout import managed_vault_path
    from .vault_deletion import _remove_catalog

    database = database or _database()
    lock = _vault_lock(vault_id)
    if not lock.acquire(blocking=False):
        raise CancelRefused("busy")
    try:
        with database() as connection:
            row = connection.execute(
                f"{_ROW_SQL} WHERE v.id=%s FOR UPDATE OF v", (vault_id,)).fetchone()
            if row is None:
                raise LookupError("Vault not found")
            if row["lifecycle"] != "provisioning":
                raise CancelRefused("not_provisioning")
            left = _bucket_left(row)
            # A provisioning Vault has no uploads or Jobs; any local catalog
            # row goes in foreign-key order, like a Vault deletion's.
            _remove_catalog(connection, int(vault_id))
            connection.execute("DELETE FROM vaults WHERE id=%s", (vault_id,))
            record_audit_event(
                connection, event="vault.creation_cancelled", actor=actor,
                vault_id=int(vault_id), outcome="success", vault_uuid=str(row["uuid"]),
                vault_name=row["name"], bucket=row["bucket"], region=row["region"],
                bucket_left=left, stage=row["stage"],
                failed_attempts=int(row["attempts"] or 0))
        managed = str(managed_vault_path(str(row["uuid"])))
        if row["source_root"] == managed:
            try:
                os.rmdir(managed)  # Only when empty: never removes content.
            except OSError:
                pass
    finally:
        lock.release()
    return {"vault_id": int(vault_id), "vault_name": row["name"], "bucket": row["bucket"],
            "region": row["region"], "bucket_left": left}


def sync_lifecycle(connection: Any, client: Any, *, bucket: str) -> int:
    """Make the Vault Bucket's lifecycle its Vault's Storage Plan; returns plan rules.

    Reads first and writes only when the lifecycle differs: the Storage Plan
    changed (or its earlier write failed), or someone edited the bucket's
    lifecycle, which is FrostVault's alone. A provisioning Vault is left to
    ``provision``.
    """
    row = connection.execute(
        f"{_ROW_SQL} WHERE t.bucket=%s AND t.key_layout=%s ORDER BY v.id", (bucket, KEY_LAYOUT),
    ).fetchall()
    if len(row) != 1:
        raise ValueError("A Vault Bucket belongs to exactly one Vault")
    vault = row[0]
    current = _spec_for(vault)
    plan_rules = len(current.lifecycle_rules) - 1
    if vault["lifecycle"] == "provisioning":
        return plan_rules
    if not buckets.lifecycle_check(client, current).ok:
        buckets.apply_lifecycle(client, current)
    return plan_rules
