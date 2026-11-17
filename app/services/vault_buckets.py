"""The dedicated Vault Bucket FrostVault creates for every new Vault (#735).

ADR-0049. A new Vault gets ``frostvault-<slug>-<8 random [a-z0-9]>`` in
``AWS_DEFAULT_REGION``: the slug is the Vault name in lowercase ASCII, at most
40 characters. Its objects live at the bucket root (the target's
``key_layout`` is ``bucket_root``, so its prefix is empty). The bucket gets the
configuration every FrostVault bucket has (``bucket_provisioning``: Block
Public Access, versioning, SSE-S3, ``BucketOwnerEnforced``), an unfiltered
lifecycle that is exactly the Vault's Storage Plan plus the incomplete
multipart abort, with ``TransitionDefaultMinimumObjectSize`` 128 KiB, and the
tags ``frostvault:vault=<uuid>`` and ``frostvault:name=<name>``. An optional
Governance Retention Lock is chosen when the Vault is created (#736).

Creation is two steps. ``reserve`` saves the generated name as the Vault's
target and the Vault as ``provisioning``, before any S3 request. ``provision``
then creates, configures and verifies the bucket; only a passing verification
makes the Vault ``active``. Both are idempotent: an interrupted creation
resumes on the same name (``BucketAlreadyOwnedByYou`` means already created)
from the request that started it, a retry by the Owner, or the background
loop after a restart (``resume_all``). A name owned by another AWS account is
replaced (at most three in a row), never probed. While a creation fails, the
Vault stays provisioning, keeps its last error and has an open Problem: its
bucket may already exist without being in use.

The lifecycle is written whole (``bucket_provisioning.apply_lifecycle``) only
by ``provision`` and by ``sync_lifecycle`` when it differs from the Storage
Plan; ``sync_lifecycle`` never touches the rest of the configuration.
"""
from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from botocore.exceptions import BotoCoreError, ClientError

from . import bucket_provisioning as buckets
from .lifecycle_profiles import LifecycleProfile, profile_from_json
from .s3_lifecycle_rules import vault_bucket_lifecycle_rules

KEY_LAYOUT = "bucket_root"
SLUG_MAX_LENGTH = 40
# The System Bucket is frostvault-system-<8>: a Vault never takes that shape.
_RESERVED_SLUGS = {"system": "system-vault"}
_NAME_ATTEMPTS = 3
PROBLEM_EVENT = "vault_bucket_failed"
# While a Vault Bucket is not ready the background loop retries this often.
POLL_SECONDS = 15 * 60
# Characters AWS accepts in a tag value besides letters, digits and spaces.
_TAG_PUNCTUATION = frozenset("+-=._:/@")
TAG_VAULT = "frostvault:vault"
TAG_NAME = "frostvault:name"

_locks_guard = threading.Lock()
_locks: dict[int, threading.Lock] = {}


class VaultBucketUnavailable(RuntimeError):
    """Vault Buckets cannot be created on this server (region or credentials)."""


def slug(name: str) -> str:
    """The Vault name as a bucket-name fragment: lowercase ASCII, at most 40.

    Accents are dropped, every other run of characters becomes one hyphen; a
    name with no ASCII letter or digit becomes ``vault``.
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
    return _RESERVED_SLUGS.get(value, value)


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
    SELECT v.id, v.uuid, v.name, v.lifecycle, v.target_id,
           v.storage_plan::text AS storage_plan_json,
           t.bucket, t.region, t.key_layout, t.retention_lock_days,
           p.stage, p.attempts, p.last_attempt_at, p.last_error, p.requested_at
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


def _public(row: dict[str, Any]) -> dict[str, Any]:
    provisioning = row["lifecycle"] == "provisioning"
    if not provisioning:
        state = "ready"
    elif row["last_error"]:
        state = "failed"
    else:
        state = "creating"
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


def _sdk_failure(action: str, exc: BaseException) -> _Failed:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code") or "ClientError")
        return _Failed(f"S3 refused {action} ({code})")
    # Provider messages can name accounts or endpoints: the type is enough.
    return _Failed(f"S3 could not be reached during {action} ({type(exc).__name__})")


def provision(vault_id: int, *, database: Callable[[], Any] | None = None,
              client: Any = None, client_for: Callable[[str], Any] | None = None,
              ) -> dict[str, Any]:
    """Create, configure and verify the Vault's bucket, then activate the Vault.

    Returns the bucket state (``status``). A failure is recorded on the Vault,
    with a Problem, and returned, not raised; the Vault stays provisioning.
    A Vault that is not provisioning is returned as it is.
    """
    database = database or _database()
    with _vault_lock(vault_id):
        with database() as connection:
            row = connection.execute(f"{_ROW_SQL} WHERE v.id=%s", (vault_id,)).fetchone()
        if row is None:
            raise LookupError("Vault not found")
        if row["lifecycle"] != "provisioning":
            return _public(row)
        try:
            if client is None:
                client = client_for_region(row["region"], client_for)
            row = _create(database, client, row)
            current = _spec_for(row)
            try:
                buckets.configure_bucket(client, current)
            except buckets.BucketProvisioningError as exc:
                raise _Failed(str(exc)) from None
            _set_stage(database, row, "configured")
            verification = buckets.verify_bucket(client, current)
            if not verification.ok:
                raise _Failed("Verification failed: " + verification.failure_message())
        except VaultBucketUnavailable as exc:
            return _record_failure(database, row, str(exc))
        except _Failed as exc:
            return _record_failure(database, row, exc.message)
        except (ClientError, BotoCoreError) as exc:
            return _record_failure(database, row, _sdk_failure("the bucket creation", exc).message)
        return _activate(database, row)


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
                failed_attempts=int(attempts["attempts"]) if attempts else 0)
        set_problem(connection, key=f"vault_bucket:{row['id']}", event=PROBLEM_EVENT,
                    vault_id=int(row["id"]), active=False)
        return status(connection, int(row["id"])) or _public(row)


def resume_all(*, database: Callable[[], Any] | None = None,
               client_for: Callable[[str], Any] | None = None) -> list[dict[str, Any]]:
    """Continue every unfinished Vault Bucket creation (after a restart or a failure)."""
    database = database or _database()
    with database() as connection:
        ids = [int(row["id"]) for row in connection.execute(
            "SELECT id FROM vaults WHERE lifecycle='provisioning' ORDER BY id").fetchall()]
    return [provision(vault_id, database=database, client_for=client_for) for vault_id in ids]


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
