"""Shared helpers for S3-compatible integrity integration tests (issue #13)."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from cryptography.fernet import Fernet

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.s3_prefix_cleanup import cleanup_prefix_versions
from app.services.vault_crypto import encrypt_vault_secrets, generate_crypt_secrets
from app.storage import s3_client
from tests.test_database import run_alembic


def require_s3_env() -> dict[str, str]:
    endpoint = os.environ["TEST_S3_ENDPOINT"]
    env = {
        "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
        "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    # Sentinel ``aws`` means public AWS (OIDC session); do not override endpoint.
    if endpoint.lower() != "aws":
        env["AWS_ENDPOINT_URL"] = endpoint
    session_token = os.getenv("AWS_SESSION_TOKEN")
    if session_token:
        env["AWS_SESSION_TOKEN"] = session_token
    return env


def s3_provider() -> str:
    return os.getenv("TEST_S3_PROVIDER", "Minio")


def prefix_root() -> str:
    return os.getenv("TEST_S3_PREFIX_ROOT", "").strip().strip("/")


def namespaced_prefix(leaf: str) -> str:
    root = prefix_root()
    leaf = leaf.strip().strip("/")
    return f"{root}/{leaf}" if root else leaf


def ensure_versioned_bucket(bucket: str) -> None:
    client = s3_client()
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        # Real AWS CI buckets are pre-provisioned; only auto-create for MinIO.
        if s3_provider().lower() != "minio":
            raise
        client.create_bucket(Bucket=bucket)
    if s3_provider().lower() == "minio":
        client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        return
    status = client.get_bucket_versioning(Bucket=bucket).get("Status")
    if status != "Enabled":
        raise RuntimeError(
            f"AWS CI bucket {bucket!r} must have versioning Enabled (found {status!r})"
        )


def write_s3_rclone_config(
    path: Path,
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str | None = None,
    prefix: str | None = None,
    remote_name: str = "ci-plain",
    base_name: str = "ci-s3",
    with_plain_alias: bool = True,
    upload_cutoff: str | None = None,
) -> tuple[str, str]:
    """Write S3 base remote and optional plain alias.

    Returns ``(plain_or_base_remote_name, base_remote_name)``.
    """
    provider = s3_provider()
    cutoff_line = (
        f"\nupload_cutoff = {upload_cutoff}" if upload_cutoff else ""
    )
    if provider.lower() == "aws" or endpoint.lower() == "aws":
        base = textwrap.dedent(
            f"""\
            [{base_name}]
            type = s3
            provider = AWS
            env_auth = true
            region = {os.getenv("AWS_DEFAULT_REGION", "eu-south-1")}
            location_constraint = {os.getenv("AWS_DEFAULT_REGION", "eu-south-1")}
            acl = private
            no_check_bucket = true{cutoff_line}
            """
        ).rstrip()
    else:
        host = endpoint.rstrip("/")
        # access_key/secret_key stay on the signature for callers; MinIO remotes
        # use env_auth so secrets are never written into the rclone config file.
        base = textwrap.dedent(
            f"""\
            [{base_name}]
            type = s3
            provider = Minio
            env_auth = true
            endpoint = {host}
            region = {os.getenv("AWS_DEFAULT_REGION", "us-east-1")}
            acl = private
            force_path_style = true{cutoff_line}
            """
        ).rstrip()
    sections = [base]
    active = base_name
    if with_plain_alias:
        if not bucket or prefix is None:
            raise ValueError("plain alias requires bucket and prefix")
        sections.append(
            textwrap.dedent(
                f"""\
                [{remote_name}]
                type = alias
                remote = {base_name}:{bucket}/{prefix.strip("/")}
                """
            ).rstrip()
        )
        active = remote_name
    path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return active, base_name


def write_plain_rclone_config(
    path: Path,
    *,
    endpoint: str,
    bucket: str,
    prefix: str,
    access_key: str,
    secret_key: str,
    remote_name: str = "ci-plain",
    base_name: str = "ci-s3",
    upload_cutoff: str | None = None,
) -> str:
    """Write an alias remote whose root is ``bucket/prefix``; return remote name."""
    remote, _base = write_s3_rclone_config(
        path,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        prefix=prefix,
        remote_name=remote_name,
        base_name=base_name,
        with_plain_alias=True,
        upload_cutoff=upload_cutoff,
    )
    return remote


def prepare_plain_vault(
    root: Path,
    *,
    relative_path: str,
    payload: bytes,
    bucket: str,
    prefix: str,
    rclone_remote: str,
) -> tuple[Path, Path]:
    source = root / "source"
    source.mkdir()
    target = source / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    with SQLiteConnection(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO users(
                id, username, display_name, password_hash, is_admin
            ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
            """
        )
        connection.execute(
            """
            INSERT INTO vaults(
                id, slug, name, source_root, s3_bucket, s3_prefix,
                rclone_remote, encryption_mode
            ) VALUES (2, 'ci', 'CI', %s, %s, %s, %s, 'plain')
            """,
            (str(source), bucket, prefix.strip("/"), rclone_remote),
        )
        ArchiveCatalog(connection).observe_local_copy(
            vault_id=2,
            path=relative_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=target.stat().st_mtime_ns,
            observed_at="2026-07-22T12:00:00+00:00",
        )
    return source, database_path


def prepare_crypt_vault(
    root: Path,
    *,
    relative_path: str,
    payload: bytes,
    bucket: str,
    prefix: str,
    base_remote: str,
    master_key: str,
) -> tuple[Path, Path]:
    source = root / "source"
    source.mkdir()
    target = source / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    secrets = generate_crypt_secrets()
    stored = encrypt_vault_secrets(secrets, master_key)
    with SQLiteConnection(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO users(
                id, username, display_name, password_hash, is_admin
            ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
            """
        )
        connection.execute(
            """
            INSERT INTO vaults(
                id, slug, name, source_root, s3_bucket, s3_prefix,
                rclone_remote, encryption_mode,
                crypt_password_ciphertext, crypt_password2_ciphertext,
                recovery_custody_confirmed_at
            ) VALUES (
                2, 'ci-crypt', 'CI Crypt', %s, %s, %s, %s, 'crypt',
                %s, %s, '2026-07-22T11:00:00+00:00'
            )
            """,
            (
                str(source),
                bucket,
                prefix.strip("/"),
                base_remote,
                stored.password_ciphertext,
                stored.password2_ciphertext,
            ),
        )
        ArchiveCatalog(connection).observe_local_copy(
            vault_id=2,
            path=relative_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=target.stat().st_mtime_ns,
            observed_at="2026-07-22T12:00:00+00:00",
        )
    return source, database_path


def new_master_key() -> str:
    return Fernet.generate_key().decode("ascii")


def force_cleanup(bucket: str, prefix: str) -> None:
    cleanup_prefix_versions(s3_client(), bucket=bucket, prefix=prefix)
