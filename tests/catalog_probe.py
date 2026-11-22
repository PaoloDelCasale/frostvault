"""Catalog lookups that only tests need (#791).

Production reads Vault Files through the browse, target and worker queries of
``ArchiveCatalog``. Tests also need to look at one file's whole explicit state,
at its Path History and at an unpaged listing; those reads live here so the
catalog's interface shows production callers only.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.catalog import VISIBLE_SQL, ArchiveCatalog, _hex, _id

# Columns of one Vault File row, in the order ``_file_dict`` reads them.
_FILE_COLUMNS_SQL = """
    f.id, f.vault_id, f.path, f.path_since, f.generation,
    f.local_presence, f.local_file_type, f.local_size, f.local_mtime_ns,
    f.local_sha256, f.local_seen_at, f.local_observed_at,
    f.local_content_observed_at, f.local_recovered_at,
    f.matched_version_id, f.held_version_id, f.pending_since,
    f.reconciliation_block, f.reconciliation_blocked_at, f.cloud_since,
    f.current_version_id, f.replacing_version_id, f.created_at,
    cur.object_key AS cur_object_key,
    cur.provider_version_id AS cur_provider_version_id,
    cur.size AS cur_size, cur.storage_class AS cur_storage_class,
    cur.plaintext_sha256 AS cur_plaintext_sha256, cur.integrity AS cur_integrity,
    cur.availability AS cur_availability, cur.verified_at AS cur_verified_at,
    cur.created_at AS cur_created_at,
    rep.object_key AS rep_object_key,
    rep.provider_version_id AS rep_provider_version_id,
    rep.size AS rep_size, rep.storage_class AS rep_storage_class,
    rep.plaintext_sha256 AS rep_plaintext_sha256, rep.integrity AS rep_integrity,
    rep.availability AS rep_availability, rep.verified_at AS rep_verified_at,
    rep.created_at AS rep_created_at
"""
_FILE_JOINS_SQL = """
    LEFT JOIN archive_versions cur ON cur.id = f.current_version_id
    LEFT JOIN archive_versions rep ON rep.id = f.replacing_version_id
"""


def _version_dict(row: Mapping[str, Any], prefix: str, version_id: Any) -> dict[str, Any] | None:
    if version_id is None:
        return None
    return {
        "id": str(version_id),
        "object_key": row[f"{prefix}_object_key"],
        "provider_version_id": row[f"{prefix}_provider_version_id"],
        "size": row[f"{prefix}_size"],
        "storage_class": row[f"{prefix}_storage_class"],
        "plaintext_sha256": _hex(row[f"{prefix}_plaintext_sha256"]),
        "integrity": row[f"{prefix}_integrity"],
        "availability": row[f"{prefix}_availability"],
        "verified_at": row[f"{prefix}_verified_at"],
        "created_at": row[f"{prefix}_created_at"],
    }


def _file_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """One Vault File with its explicit states, converted once at the boundary."""
    local_copy = None
    if row["local_presence"] is not None:
        local_copy = {
            "presence": row["local_presence"],
            "file_type": row["local_file_type"],
            "size": row["local_size"],
            "mtime_ns": row["local_mtime_ns"],
            "plaintext_sha256": _hex(row["local_sha256"]),
            "seen_at": row["local_seen_at"],
            "observed_at": row["local_observed_at"],
            "content_observed_at": row["local_content_observed_at"],
            "recovered_at": row["local_recovered_at"],
        }
    block = None
    if row["reconciliation_block"] is not None:
        block = {
            "reason": row["reconciliation_block"],
            "blocked_at": row["reconciliation_blocked_at"],
        }
    return {
        "id": str(row["id"]),
        "vault_id": int(row["vault_id"]),
        "path": row["path"],
        "path_since": row["path_since"],
        "generation": int(row["generation"]),
        "local_copy": local_copy,
        "current_version": _version_dict(row, "cur", row["current_version_id"]),
        "replacing_version": _version_dict(row, "rep", row["replacing_version_id"]),
        "matched_version_id": _id(row["matched_version_id"]),
        "held_version_id": _id(row["held_version_id"]),
        "pending_since": row["pending_since"],
        "reconciliation_block": block,
        "cloud_since": row["cloud_since"],
        "created_at": row["created_at"],
    }


def get_file(catalog: ArchiveCatalog, vault_file_id: str, *, vault_id: int) -> dict[str, Any] | None:
    row = catalog.connection.execute(
        f"SELECT {_FILE_COLUMNS_SQL} FROM files f {_FILE_JOINS_SQL} "
        "WHERE f.id=%s AND f.vault_id=%s",
        (vault_file_id, vault_id),
    ).fetchone()
    return None if row is None else _file_dict(row)


def get_file_by_path(catalog: ArchiveCatalog, vault_id: int, path: str) -> dict[str, Any] | None:
    row = catalog.connection.execute(
        f"SELECT {_FILE_COLUMNS_SQL} FROM files f {_FILE_JOINS_SQL} "
        "WHERE f.vault_id=%s AND f.path=%s",
        (vault_id, path),
    ).fetchone()
    return None if row is None else _file_dict(row)


def list_path_history(
    catalog: ArchiveCatalog, vault_file_id: str, *, vault_id: int
) -> list[dict[str, Any]]:
    """Every path of the file in order: closed mappings, then the current one."""
    rows = catalog.connection.execute(
        """
        SELECT path, valid_from, valid_to, 0 AS rank, id
        FROM file_path_history
        WHERE file_id=%s AND vault_id=%s
        UNION ALL
        SELECT path, path_since, NULL, 1, NULL
        FROM files
        WHERE id=%s AND vault_id=%s
        ORDER BY rank, valid_from, id
        """,
        (vault_file_id, vault_id, vault_file_id, vault_id),
    ).fetchall()
    return [
        {"path": row["path"], "valid_from": row["valid_from"], "valid_to": row["valid_to"]}
        for row in rows
    ]


def list_file_rows(
    catalog: ArchiveCatalog,
    vault_id: int,
    *,
    search: str = "",
    path_prefix: str = "",
) -> list[dict[str, Any]]:
    """Every visible file of the Vault as browse items, unpaged."""
    clauses = ["f.vault_id=%s"]
    params: list[Any] = [vault_id]
    if search:
        clauses.append("lower(f.path) LIKE lower(%s)")
        params.append(f"%{search}%")
    if path_prefix:
        clauses.append("starts_with(f.path, %s)")
        params.append(f"{path_prefix}/")
    clauses.append(VISIBLE_SQL)
    rows = catalog.connection.execute(
        catalog._file_row_select_sql(clauses, vault_id) + " ORDER BY lower(f.path), f.path", params,
    ).fetchall()
    return catalog._materialize_file_items(vault_id, rows, name_from_path=True)
