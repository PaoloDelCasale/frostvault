"""Rows of the v0.6 baseline that auth, audit and notification tests share.

The Owner is the single ``owner`` row (id 1); a Vault needs a Vault Target.
"""
from __future__ import annotations

from typing import Any

from tests.fixtures import create_owner, seed_vault


# Aliases of ``tests.fixtures`` for files not yet migrated (#801).
insert_owner = create_owner
insert_vault = seed_vault


def bump_session_version(connection: Any) -> None:
    connection.execute("UPDATE owner SET session_version = session_version + 1 WHERE id = 1")


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
