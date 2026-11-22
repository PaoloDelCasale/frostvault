"""The installation's System Bucket for metadata backups (#734, ADR-0049).

``METADATA_BACKUP_BUCKET``, when set, names an operator-supplied bucket that
FrostVault verifies (Block Public Access, versioning, SSE-S3, region) and never
changes. Otherwise FrostVault creates ``frostvault-system-<8 random [a-z0-9]>``
once, in ``AWS_DEFAULT_REGION``, with the same configuration, Object Ownership
``BucketOwnerEnforced``, a lifecycle that aborts incomplete multipart uploads,
the tag ``frostvault:role=system`` and no Object Lock: backup retention must be
able to delete. Afterwards each backup verifies the baseline, Object Ownership,
the presence of the abort rule and the absence of Object Lock; lifecycle rules
and tags the Owner adds are accepted.

The generated name is saved before the bucket is created, so an interrupted
creation resumes on the same name and never leaves an unknown bucket behind.
The row records the region the bucket was created in; changing
``AWS_DEFAULT_REGION`` later does not move it.

Without ``METADATA_BACKUP_BUCKET``, usable AWS credentials and any Vault
there is no System Bucket: metadata backups stay local, as in development.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from ..config import is_placeholder, settings
from . import bucket_provisioning as buckets
from .s3_lifecycle_rules import ABORT_RULE_ID

NAME_STEM = "frostvault-system"
ROLE_TAGS = {"frostvault:role": "system"}
ABORT_RULE = {
    "ID": ABORT_RULE_ID,
    "Status": "Enabled",
    "Filter": {"Prefix": ""},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
}
# A name another AWS account owns is replaced; three in a row is not chance.
_NAME_ATTEMPTS = 3
_ensure_lock = threading.Lock()


class SystemBucketUnavailable(RuntimeError):
    """The System Bucket cannot be used; the message is safe to store and show."""


class ReservationChanged(SystemBucketUnavailable):
    """Another process renamed or completed the saved name first."""


@dataclass(frozen=True)
class SystemBucket:
    name: str
    region: str
    # ``configured``: METADATA_BACKUP_BUCKET. ``created``: made by FrostVault.
    origin: str
    # False while a created bucket's creation has not completed and verified.
    ready: bool = True

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "region": self.region,
                "origin": self.origin, "ready": self.ready}


@dataclass(frozen=True)
class Reservation:
    name: str
    region: str
    ready: bool


class SystemBucketStore(Protocol):
    """Durable state of the FrostVault-created System Bucket (one row)."""

    def load(self) -> Reservation | None: ...

    def reserve(self, name: str, region: str) -> Reservation: ...

    def rename(self, old: str, new: str) -> Reservation:
        """Replace a not-ready name; raise ``ReservationChanged`` if ``old`` is gone."""

    def mark_ready(self, name: str) -> None:
        """Mark ``name`` ready; raise ``ReservationChanged`` unless it is the saved name."""


def configured_bucket(cfg: Any = None) -> str | None:
    """The operator's METADATA_BACKUP_BUCKET, unless empty or a placeholder."""
    cfg = cfg if cfg is not None else settings
    name = str(getattr(cfg, "metadata_backup_bucket", "") or "").strip()
    if not name or is_placeholder(name, "example-bucket", "BUCKET-NAME"):
        return None
    return name


# Credential sources named by the environment besides static keys. An
# instance role alone is not probed: that would query the metadata service.
_CREDENTIAL_SOURCE_VARIABLES = (
    "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


def aws_credentials_configured() -> bool:
    """Whether the environment gives the SDK real credentials, without probing AWS.

    Static keys must both be set and not be the documented placeholders;
    otherwise a profile, web identity or container credential variable counts.
    """
    access = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    if access or secret:
        return bool(access and secret) and not (
            is_placeholder(access, "REPLACE_ME", "REPLACE-WITH-ACCESS-KEY")
            or is_placeholder(secret, "REPLACE_ME", "REPLACE-WITH-SECRET-KEY"))
    return any(os.getenv(name, "").strip() for name in _CREDENTIAL_SOURCE_VARIABLES)


def vaults_exist(connection: Any = None) -> bool:
    """Whether the catalog holds any Vault, whatever its lifecycle (#735).

    A Vault keeps content in its Vault Bucket (a Tombstone too, until its
    Vault Deletion), so the catalog that names those buckets must leave the
    server: missing credentials or IAM then fail the backup loudly instead of
    silently downgrading it to local-only.
    """
    query = "SELECT 1 FROM vaults LIMIT 1"
    if connection is not None:
        return connection.execute(query).fetchone() is not None
    from ..database import db

    with db() as own:
        return own.execute(query).fetchone() is not None


def off_host_required(cfg: Any = None, *,
                      credentials: Callable[[], bool] | None = None,
                      vaults: Callable[[], bool] | None = None) -> bool:
    """Whether metadata backups must also reach the System Bucket."""
    return (configured_bucket(cfg) is not None
            or (credentials or aws_credentials_configured)()
            or (vaults or vaults_exist)())


def spec(name: str, region: str) -> buckets.BucketSpec:
    return buckets.BucketSpec(name=name, region=region, tags=dict(ROLE_TAGS),
                              lifecycle_rules=(ABORT_RULE,), retention_lock=None)


def location(cfg: Any = None, *, connection: Any = None,
             store: SystemBucketStore | None = None) -> SystemBucket | None:
    """The System Bucket the Recovery Kit names, without any S3 request.

    ``connection`` reads the saved row inside the caller's transaction.
    """
    cfg = cfg if cfg is not None else settings
    name = configured_bucket(cfg)
    region = str(getattr(cfg, "aws_region", "") or "").strip()
    if name is not None:
        return SystemBucket(name, region, "configured") if region else None
    if connection is not None:
        reservation = DatabaseStore.read(connection)
    else:
        reservation = (store or DatabaseStore()).load()
    if reservation is None:
        return None
    return SystemBucket(reservation.name, reservation.region, "created", reservation.ready)


def verify_created(client: Any, reservation: Reservation) -> buckets.BucketVerification:
    """A created System Bucket: baseline, ownership, abort rule, no Object Lock.

    Extra lifecycle rules (e.g. noncurrent-version expiry) and tags the Owner
    adds are accepted; FrostVault's abort rule must stay as it was created.
    """
    return buckets.verify_bucket(client, spec(reservation.name, reservation.region),
                                 extra_lifecycle_rules=True, require_tags=False)


def ensure(cfg: Any = None, *, store: SystemBucketStore | None = None,
           client_for: Callable[[str], Any] | None = None) -> SystemBucket:
    """The verified System Bucket, creating FrostVault's one when needed.

    Every call verifies the bucket again before a backup relies on it; an
    operator-supplied bucket is only read, never changed. Only creation holds
    the process lock; verifying a ready bucket does not.
    """
    cfg = cfg if cfg is not None else settings
    client_for = client_for or _default_client
    region = str(getattr(cfg, "aws_region", "") or "").strip()
    name = configured_bucket(cfg)
    if name is not None:
        if not region:
            raise SystemBucketUnavailable("METADATA_BACKUP_BUCKET needs AWS_DEFAULT_REGION")
        verification = buckets.verify_baseline(client_for(region), name, region)
        if not verification.ok:
            raise SystemBucketUnavailable(
                "METADATA_BACKUP_BUCKET refused: " + verification.failure_message()
                + ". Enable Block Public Access, versioning and SSE-S3 on it, in "
                "AWS_DEFAULT_REGION, or unset it to let FrostVault create its own")
        return SystemBucket(name, region, "configured")
    store = store or DatabaseStore()
    reservation = store.load()
    if reservation is None or not reservation.ready:
        with _ensure_lock:
            reservation = store.load()
            if reservation is None:
                if not region:
                    raise SystemBucketUnavailable(
                        "Creating the System Bucket needs AWS_DEFAULT_REGION")
                # A client first: without credentials no name is saved.
                client_for(region)
                reservation = store.reserve(buckets.random_bucket_name(NAME_STEM), region)
            if not reservation.ready:
                return _finish_creation(store, client_for(reservation.region), reservation)
    # A ready bucket is never repaired or recreated: a change, or its deletion,
    # is refused until the Owner restores it.
    verification = verify_created(client_for(reservation.region), reservation)
    if not verification.ok:
        raise SystemBucketUnavailable(
            f"System Bucket {reservation.name} failed verification: "
            + verification.failure_message())
    return SystemBucket(reservation.name, reservation.region, "created")


def _finish_creation(store: SystemBucketStore, client: Any,
                     reservation: Reservation) -> SystemBucket:
    for _ in range(_NAME_ATTEMPTS):
        try:
            buckets.provision_bucket(client, spec(reservation.name, reservation.region))
        except buckets.BucketNameUnavailable:
            reservation = store.rename(reservation.name, buckets.random_bucket_name(NAME_STEM))
            continue
        except buckets.BucketProvisioningError as exc:
            raise SystemBucketUnavailable(
                f"Creating System Bucket {reservation.name} did not finish: {exc}. "
                "FrostVault retries with the same name") from None
        store.mark_ready(reservation.name)
        return SystemBucket(reservation.name, reservation.region, "created")
    raise SystemBucketUnavailable(
        "Every generated System Bucket name belongs to another AWS account; "
        "set METADATA_BACKUP_BUCKET or retry later")


def _default_client(region: str) -> Any:
    from ..storage import s3_client

    return s3_client(region=region)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatabaseStore:
    """The ``system_bucket`` row; every method commits on its own."""

    def __init__(self, database: Callable[[], Any] | None = None):
        if database is None:
            from ..database import db as database
        self._database = database

    @staticmethod
    def read(connection: Any) -> Reservation | None:
        row = connection.execute(
            "SELECT name, region, ready_at FROM system_bucket WHERE id=1").fetchone()
        if row is None:
            return None
        return Reservation(str(row["name"]), str(row["region"]), row["ready_at"] is not None)

    def load(self) -> Reservation | None:
        with self._database() as connection:
            return self.read(connection)

    def reserve(self, name: str, region: str) -> Reservation:
        # Two starts racing keep the first name; the loser adopts it.
        with self._database() as connection:
            connection.execute(
                """INSERT INTO system_bucket(id, name, region, reserved_at)
                   VALUES (1, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                (name, region, _now()))
            return self.read(connection)

    def rename(self, old: str, new: str) -> Reservation:
        with self._database() as connection:
            changed = connection.execute(
                "UPDATE system_bucket SET name=%s WHERE id=1 AND name=%s AND ready_at IS NULL",
                (new, old)).rowcount
            if changed != 1:
                raise ReservationChanged(
                    f"The System Bucket name {old} changed while FrostVault created it; "
                    "the next attempt uses the saved name")
            return self.read(connection)

    def mark_ready(self, name: str) -> None:
        with self._database() as connection:
            changed = connection.execute(
                "UPDATE system_bucket SET ready_at=%s WHERE id=1 AND name=%s AND ready_at IS NULL",
                (_now(), name)).rowcount
            if changed == 1:
                return
            # Another process may have completed the same name first: fine.
            current = self.read(connection)
            if current is None or current.name != name or not current.ready:
                raise ReservationChanged(
                    f"The System Bucket name {name} changed while FrostVault created it; "
                    "the next attempt uses the saved name")
