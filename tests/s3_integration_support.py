"""Helpers for plaintext S3-compatible integrity proofs (CI-only buckets)."""
from __future__ import annotations

import os
from pathlib import Path

from app.services.local_copies import LocalCopies

from app.services.s3_prefix_cleanup import cleanup_prefix_versions
from app.storage import s3_client
from tests.fixtures import Database, seed_vault


def require_s3_env() -> dict[str, str]:
    endpoint = os.environ["TEST_S3_ENDPOINT"]
    env = {
        "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
        "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    if endpoint.lower() != "aws":
        env["AWS_ENDPOINT_URL"] = endpoint
    if os.getenv("AWS_SESSION_TOKEN"):
        env["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
    return env


def s3_provider() -> str:
    return os.getenv("TEST_S3_PROVIDER", "Minio")


def prefix_root() -> str:
    return os.getenv("TEST_S3_PREFIX_ROOT", "").strip().strip("/")


def namespaced_prefix(leaf: str) -> str:
    root = prefix_root()
    return f"{root}/{leaf.strip('/')}" if root else leaf.strip("/")


def ensure_versioned_bucket(bucket: str) -> None:
    client = s3_client()
    # Never provision/change a real AWS bucket. The CI-compatible server is
    # disposable, and must actually implement BPA; no preflight mocks/bypass.
    if s3_provider().lower() == "minio":
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        client.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        })
    from app.services.s3_preflight import check_bucket_readiness, preflight_failure_message
    result = check_bucket_readiness(bucket, region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"), client=client)
    if not result.ok:
        raise RuntimeError(preflight_failure_message(result))


def prepare_plain_vault(
    database: Database, root: Path, *, relative_path: str, payload: bytes, bucket: str,
) -> tuple[Path, str]:
    """One Vault (id 2) and its Local Copy in ``database``, which holds the Owner.

    Returns the source root and the Vault namespace ``vaults/<uuid>`` its
    objects live under (ADR-0035: no custom prefixes).
    """
    source = root / "source"
    source.mkdir()
    target = source / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    with database.connect() as connection:
        vault_uuid = seed_vault(
            connection, id=2, name="CI", source_root=str(source), bucket=bucket,
            target={"region": os.getenv("AWS_DEFAULT_REGION", "us-east-1")},
        )["uuid"]
        from app.services.vault_relocation import enroll_vault_root_identity
        enroll_vault_root_identity(connection, 2, str(source))
        LocalCopies(connection).observe_local_copy(
            vault_id=2, path=relative_path, file_type="regular", size=len(payload),
            mtime_ns=target.stat().st_mtime_ns, observed_at="2026-07-22T12:00:00+00:00",
        )
    return source, f"vaults/{vault_uuid}"


def force_cleanup(bucket: str, prefix: str) -> None:
    cleanup_prefix_versions(s3_client(), bucket=bucket, prefix=prefix)
