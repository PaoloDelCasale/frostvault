"""Rows of the v0.6 baseline that auth, audit and notification tests share.

The Owner is the single ``owner`` row (id 1); a Vault needs a Vault Target.
"""
from __future__ import annotations

from typing import Any

from app.security import hash_password


def insert_owner(
    connection: Any,
    *,
    display_name: str = "Owner",
    password: str | None = None,
    password_hash: str | None = "hash",
) -> None:
    """Create the single Owner, with an Emergency Password when given."""
    if password is not None:
        password_hash = hash_password(password)
    connection.execute(
        "INSERT INTO owner(id, display_name, password_hash) VALUES (1, %s, %s)",
        (display_name, password_hash),
    )


def bump_session_version(connection: Any) -> None:
    connection.execute("UPDATE owner SET session_version = session_version + 1 WHERE id = 1")


def insert_vault(
    connection: Any,
    *,
    name: str = "Docs",
    source_root: str = "/sources/managed/docs",
    bucket: str = "bucket",
    **values: Any,
) -> dict[str, Any]:
    """Create a Vault (and its Vault Target on first use)."""
    target = connection.execute(
        "SELECT id FROM vault_targets WHERE bucket=%s", (bucket,)
    ).fetchone() or connection.execute(
        "INSERT INTO vault_targets(provider, bucket, region) "
        "VALUES ('s3', %s, 'eu-south-1') RETURNING id",
        (bucket,),
    ).fetchone()
    columns = {"name": name, "source_root": source_root, "target_id": target["id"], **values}
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return connection.execute(
        f"INSERT INTO vaults({names}) VALUES ({placeholders}) RETURNING *",
        tuple(columns.values()),
    ).fetchone()


def insert_job(
    connection: Any,
    *,
    vault_id: int,
    path: str = "a.txt",
    action: str = "upload",
    status: str = "completed",
    origin: str = "manual",
) -> int:
    """Create a Vault File at ``path`` (once) and one Job on it."""
    file = connection.execute(
        "SELECT id FROM files WHERE vault_id=%s AND path=%s", (vault_id, path)
    ).fetchone() or connection.execute(
        "INSERT INTO files(vault_id, path, path_since) VALUES (%s, %s, now()) RETURNING id",
        (vault_id, path),
    ).fetchone()
    return connection.execute(
        """
        INSERT INTO jobs(vault_id, file_id, action, status, origin)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (vault_id, file["id"], action, status, origin),
    ).fetchone()["id"]
