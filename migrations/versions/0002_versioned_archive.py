"""Introduce stable Vault Files and immutable Archive Versions.

Revision ID: 0002_versioned_archive
Revises: 0001_current_schema
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_versioned_archive"
down_revision: str | None = "0001_current_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_job_conflicts(connection: Any) -> None:
    conflicts = connection.execute(
        sa.text(
            """
            SELECT vault_id, path, COUNT(*) AS total
            FROM jobs
            WHERE status NOT IN ('completed', 'failed', 'cancelled')
            GROUP BY vault_id, path
            HAVING COUNT(*) > 1
            """
        )
    ).mappings().all()
    if conflicts:
        details = ", ".join(
            f"vault={row['vault_id']} path={row['path']} jobs={row['total']}"
            for row in conflicts
        )
        raise RuntimeError(
            "Cannot migrate while multiple non-terminal jobs target one file: "
            + details
        )


def _sync_jobs_sequence(connection: Any) -> None:
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        sa.text(
            """
            SELECT setval(
                pg_get_serial_sequence('jobs', 'id'),
                COALESCE(MAX(id), 1),
                MAX(id) IS NOT NULL
            )
            FROM jobs
            """
        )
    )


def _create_versioned_tables() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    uuid_type = sa.String(36)
    op.create_table(
        "vault_files",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('active', 'retired', 'purged')",
            name="vault_files_status_ck",
        ),
    )
    op.create_index("vault_files_vault_status_idx", "vault_files", ["vault_id", "status"])
    op.create_table(
        "file_paths",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "vault_file_id", uuid_type,
            sa.ForeignKey("vault_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Text(), nullable=False),
        sa.Column("valid_to", sa.Text()),
    )
    current_path = sa.text("valid_to IS NULL")
    op.create_index(
        "file_paths_one_current_path_uq",
        "file_paths",
        ["vault_id", "path"],
        unique=True,
        sqlite_where=current_path,
        postgresql_where=current_path,
    )
    op.create_index(
        "file_paths_one_current_file_uq",
        "file_paths",
        ["vault_file_id"],
        unique=True,
        sqlite_where=current_path,
        postgresql_where=current_path,
    )
    op.create_table(
        "archive_versions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "vault_file_id", uuid_type,
            sa.ForeignKey("vault_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.Text()),
        sa.Column("provider_version_id", sa.Text()),
        sa.Column("size", sa.BigInteger()),
        sa.Column("storage_class", sa.Text()),
        sa.Column("etag", sa.Text()),
        sa.Column("provider_checksums", sa.Text()),
        sa.Column("plaintext_sha256", sa.String(64)),
        sa.Column("uploaded_at", sa.Text()),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("integrity", sa.Text(), nullable=False, server_default="unverified"),
        sa.Column("verified_at", sa.Text()),
        sa.Column("availability", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("availability_checked_at", sa.Text()),
        sa.Column("restore_state", sa.Text()),
        sa.Column("restore_expiry", sa.Text()),
        sa.Column("restore_checked_at", sa.Text()),
        sa.Column("desired_policy_id", uuid_type),
        sa.Column("applied_policy_id", uuid_type),
        sa.UniqueConstraint(
            "vault_file_id", "version_number",
            name="archive_versions_file_number_uq",
        ),
        sa.CheckConstraint(
            "origin IN ('upload', 'discovered', 'legacy')",
            name="archive_versions_origin_ck",
        ),
        sa.CheckConstraint(
            "integrity IN ('unverified', 'verified', 'mismatch')",
            name="archive_versions_integrity_ck",
        ),
        sa.CheckConstraint(
            "availability IN ('unknown', 'available', 'missing', 'purged')",
            name="archive_versions_availability_ck",
        ),
        sa.CheckConstraint(
            "origin = 'legacy' OR provider_version_id IS NOT NULL",
            name="archive_versions_provider_id_ck",
        ),
    )
    op.create_index(
        "archive_versions_object_version_uq",
        "archive_versions",
        ["vault_id", "object_key", "provider_version_id"],
        unique=True,
    )
    op.create_index(
        "archive_versions_verification_idx",
        "archive_versions",
        ["integrity", "availability", "verified_at"],
    )
    op.create_index(
        "archive_versions_file_verification_idx",
        "archive_versions",
        ["vault_file_id", "integrity", "availability", "version_number"],
    )
    op.create_table(
        "delete_markers",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "vault_file_id", uuid_type,
            sa.ForeignKey("vault_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("provider_version_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "vault_id", "object_key", "provider_version_id",
            name="delete_markers_object_version_uq",
        ),
    )
    op.create_index(
        "delete_markers_file_created_idx",
        "delete_markers",
        ["vault_file_id", "created_at"],
    )
    op.create_table(
        "local_copies",
        sa.Column(
            "vault_file_id", uuid_type,
            sa.ForeignKey("vault_files.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("presence", sa.Text(), nullable=False),
        sa.Column("file_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger()),
        sa.Column("mtime_ns", sa.BigInteger()),
        sa.Column("plaintext_sha256", sa.String(64)),
        sa.Column(
            "matched_archive_version_id", uuid_type,
            sa.ForeignKey("archive_versions.id", ondelete="SET NULL"),
        ),
        sa.Column("last_seen_at", sa.Text()),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "presence IN ('present', 'missing', 'unsupported')",
            name="local_copies_presence_ck",
        ),
        sa.CheckConstraint(
            "file_type IN ('regular', 'symlink', 'other')",
            name="local_copies_file_type_ck",
        ),
    )
    op.create_index(
        "local_copies_presence_idx",
        "local_copies",
        ["presence", "file_type"],
    )


def _create_versioned_jobs() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    uuid_type = sa.String(36)
    op.create_table(
        "jobs",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vault_file_id", uuid_type,
            sa.ForeignKey("vault_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "archive_version_id", uuid_type,
            sa.ForeignKey("archive_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column(
            "requested_by", identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text()),
        sa.Column("group_path", sa.Text()),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "transferred_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.CheckConstraint(
            "action IN ('upload', 'recover', 'free-space')",
            name="jobs_action_check",
        ),
    )
    op.create_index(
        "jobs_vault_active_idx", "jobs", ["vault_id", "status", "action"]
    )
    active = sa.text("status NOT IN ('completed', 'failed', 'cancelled')")
    op.create_index(
        "jobs_one_active_file_uq",
        "jobs",
        ["vault_file_id"],
        unique=True,
        sqlite_where=active,
        postgresql_where=active,
    )


def _create_legacy_files() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "files",
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("local_exists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("local_size", sa.BigInteger()),
        sa.Column("local_mtime", sa.Float()),
        sa.Column("cloud_exists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cloud_size", sa.BigInteger()),
        sa.Column("cloud_key", sa.Text()),
        sa.Column("storage_class", sa.Text()),
        sa.Column("etag", sa.Text()),
        sa.Column("restore_state", sa.Text()),
        sa.Column("restore_expiry", sa.Text()),
        sa.Column("last_local_scan", sa.Text()),
        sa.Column("last_restore_scan", sa.Text()),
        sa.Column("last_cloud_scan", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "files_vault_state_idx",
        "files",
        ["vault_id", "local_exists", "cloud_exists"],
    )


def _create_legacy_jobs() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "jobs",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("vault_id", identifier, nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column(
            "requested_by", identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text()),
        sa.Column("group_path", sa.Text()),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "transferred_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.CheckConstraint(
            "action IN ('upload', 'recover', 'free-space')",
            name="jobs_action_check",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "path"],
            ["files.vault_id", "files.path"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "jobs_vault_active_idx", "jobs", ["vault_id", "status", "action"]
    )
    active = sa.text("status NOT IN ('completed', 'failed', 'cancelled')")
    op.create_index(
        "jobs_one_active_action_uq",
        "jobs",
        ["vault_id", "path", "action"],
        unique=True,
        sqlite_where=active,
        postgresql_where=active,
    )


def upgrade() -> None:
    connection = op.get_bind()
    _check_job_conflicts(connection)
    legacy_jobs = connection.execute(
        sa.text("SELECT * FROM jobs ORDER BY id")
    ).mappings().all()
    _create_versioned_tables()

    files = connection.execute(sa.text("SELECT * FROM files")).mappings().all()
    file_ids: dict[tuple[int, str], str] = {}
    archive_ids: dict[tuple[int, str], str] = {}
    for row in files:
        key = (int(row["vault_id"]), row["path"])
        vault_file_id = str(uuid.uuid4())
        file_ids[key] = vault_file_id
        timestamp = row["updated_at"] or _now()
        connection.execute(
            sa.text(
                """
                INSERT INTO vault_files(id, vault_id, status, created_at)
                VALUES (:id, :vault_id, 'active', :created_at)
                """
            ),
            {"id": vault_file_id, "vault_id": row["vault_id"], "created_at": timestamp},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO file_paths(
                    vault_file_id, vault_id, path, valid_from, valid_to
                ) VALUES (:file_id, :vault_id, :path, :valid_from, NULL)
                """
            ),
            {
                "file_id": vault_file_id,
                "vault_id": row["vault_id"],
                "path": row["path"],
                "valid_from": timestamp,
            },
        )
        mtime_ns = (
            round(float(row["local_mtime"]) * 1_000_000_000)
            if row["local_mtime"] is not None
            else None
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO local_copies(
                    vault_file_id, presence, file_type, size, mtime_ns,
                    plaintext_sha256, matched_archive_version_id,
                    last_seen_at, observed_at
                ) VALUES (
                    :file_id, :presence, 'regular', :size, :mtime_ns,
                    NULL, NULL, :last_seen_at, :observed_at
                )
                """
            ),
            {
                "file_id": vault_file_id,
                "presence": "present" if row["local_exists"] else "missing",
                "size": row["local_size"],
                "mtime_ns": mtime_ns,
                "last_seen_at": row["last_local_scan"],
                "observed_at": timestamp,
            },
        )
        if row["cloud_exists"]:
            archive_id = str(uuid.uuid4())
            archive_ids[key] = archive_id
            connection.execute(
                sa.text(
                    """
                    INSERT INTO archive_versions(
                        id, vault_file_id, vault_id, version_number, object_key,
                        provider_version_id, size, storage_class, etag,
                        provider_checksums, plaintext_sha256, uploaded_at,
                        discovered_at, origin, integrity, verified_at,
                        availability, availability_checked_at, restore_state,
                        restore_expiry, restore_checked_at, desired_policy_id,
                        applied_policy_id
                    ) VALUES (
                        :id, :file_id, :vault_id, 1, :object_key, NULL, :size,
                        :storage_class, :etag, NULL, NULL, :uploaded_at,
                        :discovered_at, 'legacy', 'unverified', NULL,
                        'unknown', NULL, :restore_state, :restore_expiry,
                        :restore_checked_at, NULL, NULL
                    )
                    """
                ),
                {
                    "id": archive_id,
                    "file_id": vault_file_id,
                    "vault_id": row["vault_id"],
                    "object_key": row["cloud_key"],
                    "size": row["cloud_size"],
                    "storage_class": row["storage_class"],
                    "etag": row["etag"],
                    "uploaded_at": row["last_cloud_scan"] or timestamp,
                    "discovered_at": row["last_cloud_scan"] or timestamp,
                    "restore_state": row["restore_state"],
                    "restore_expiry": row["restore_expiry"],
                    "restore_checked_at": row["last_restore_scan"],
                },
            )

    op.drop_index("jobs_one_active_action_uq", table_name="jobs")
    op.drop_index("jobs_vault_active_idx", table_name="jobs")
    op.drop_table("jobs")
    _create_versioned_jobs()
    for row in legacy_jobs:
        key = (int(row["vault_id"]), row["path"])
        vault_file_id = file_ids.get(key)
        if vault_file_id is None:
            raise RuntimeError(
                f"Job {row['id']} references missing legacy file {key}"
            )
        target_version = (
            archive_ids.get(key)
            if row["action"] in {"recover", "free-space"}
            else None
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO jobs(
                    id, vault_id, vault_file_id, archive_version_id, path,
                    action, status, message, requested_by, requested_at,
                    updated_at, group_id, group_path, total_bytes,
                    transferred_bytes
                ) VALUES (
                    :id, :vault_id, :file_id, :version_id, :path,
                    :action, :status, :message, :requested_by, :requested_at,
                    :updated_at, :group_id, :group_path, :total_bytes,
                    :transferred_bytes
                )
                """
            ),
            {
                "id": row["id"],
                "vault_id": row["vault_id"],
                "file_id": vault_file_id,
                "version_id": target_version,
                "path": row["path"],
                "action": row["action"],
                "status": row["status"],
                "message": row["message"],
                "requested_by": row["requested_by"],
                "requested_at": row["requested_at"],
                "updated_at": row["updated_at"],
                "group_id": row["group_id"],
                "group_path": row["group_path"],
                "total_bytes": row["total_bytes"],
                "transferred_bytes": row["transferred_bytes"],
            },
        )

    _sync_jobs_sequence(connection)
    op.drop_table("files")


def downgrade() -> None:
    connection = op.get_bind()
    incompatible = connection.execute(
        sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM delete_markers) AS delete_markers,
                (
                    SELECT COUNT(*) FROM vault_files
                    WHERE status<>'active' OR retired_at IS NOT NULL
                ) AS retired_files,
                (
                    SELECT COUNT(*) FROM (
                        SELECT vault_file_id
                        FROM file_paths
                        GROUP BY vault_file_id
                        HAVING COUNT(*)<>1 OR MAX(valid_to) IS NOT NULL
                    ) AS incompatible_paths
                ) AS path_histories,
                (
                    SELECT COUNT(*) FROM (
                        SELECT vault_file_id
                        FROM archive_versions
                        GROUP BY vault_file_id
                        HAVING COUNT(*)>1
                    ) AS multiple_versions
                ) AS multiple_versions,
                (
                    SELECT COUNT(*) FROM archive_versions
                    WHERE origin<>'legacy'
                       OR provider_version_id IS NOT NULL
                       OR provider_checksums IS NOT NULL
                       OR plaintext_sha256 IS NOT NULL
                       OR integrity<>'unverified'
                       OR desired_policy_id IS NOT NULL
                       OR applied_policy_id IS NOT NULL
                ) AS enriched_versions,
                (
                    SELECT COUNT(*) FROM archive_versions
                    WHERE availability<>'unknown'
                       OR availability_checked_at IS NOT NULL
                ) AS archive_availability,
                (
                    SELECT COUNT(*) FROM local_copies
                    WHERE file_type<>'regular'
                       OR plaintext_sha256 IS NOT NULL
                       OR matched_archive_version_id IS NOT NULL
                ) AS enriched_local_copies
            """
        )
    ).mappings().one()
    blockers = [
        name for name, total in incompatible.items() if int(total or 0) > 0
    ]
    if blockers:
        raise RuntimeError(
            "Downgrade would lose versioned archive data; incompatible features: "
            + ", ".join(blockers)
        )

    file_rows = connection.execute(
        sa.text(
            """
            SELECT
                vf.vault_id,
                fp.path,
                lc.presence,
                lc.size AS local_size,
                lc.mtime_ns,
                lc.last_seen_at,
                lc.observed_at,
                av.size AS cloud_size,
                av.object_key,
                av.storage_class,
                av.etag,
                av.restore_state,
                av.restore_expiry,
                av.restore_checked_at,
                av.discovered_at,
                vf.created_at
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
            LEFT JOIN archive_versions av ON av.vault_file_id=vf.id
            ORDER BY vf.vault_id, fp.path
            """
        )
    ).mappings().all()
    job_rows = connection.execute(sa.text("SELECT * FROM jobs")).mappings().all()

    op.drop_index("jobs_one_active_file_uq", table_name="jobs")
    op.drop_index("jobs_vault_active_idx", table_name="jobs")
    op.drop_table("jobs")
    _create_legacy_files()
    for row in file_rows:
        updated_at = (
            row["observed_at"]
            or row["discovered_at"]
            or row["created_at"]
            or _now()
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO files(
                    vault_id, path, local_exists, local_size, local_mtime,
                    cloud_exists, cloud_size, cloud_key, storage_class, etag,
                    restore_state, restore_expiry, last_local_scan,
                    last_restore_scan, last_cloud_scan, updated_at
                ) VALUES (
                    :vault_id, :path, :local_exists, :local_size, :local_mtime,
                    :cloud_exists, :cloud_size, :cloud_key, :storage_class, :etag,
                    :restore_state, :restore_expiry, :last_local_scan,
                    :last_restore_scan, :last_cloud_scan, :updated_at
                )
                """
            ),
            {
                "vault_id": row["vault_id"],
                "path": row["path"],
                "local_exists": int(row["presence"] == "present"),
                "local_size": row["local_size"],
                "local_mtime": (
                    float(row["mtime_ns"]) / 1_000_000_000
                    if row["mtime_ns"] is not None
                    else None
                ),
                "cloud_exists": int(row["object_key"] is not None),
                "cloud_size": row["cloud_size"],
                "cloud_key": row["object_key"],
                "storage_class": row["storage_class"],
                "etag": row["etag"],
                "restore_state": row["restore_state"],
                "restore_expiry": row["restore_expiry"],
                "last_local_scan": row["last_seen_at"],
                "last_restore_scan": row["restore_checked_at"],
                "last_cloud_scan": row["discovered_at"],
                "updated_at": str(updated_at),
            },
        )
    _create_legacy_jobs()
    for row in job_rows:
        connection.execute(
            sa.text(
                """
                INSERT INTO jobs(
                    id, vault_id, path, action, status, message, requested_by,
                    requested_at, updated_at, group_id, group_path, total_bytes,
                    transferred_bytes
                ) VALUES (
                    :id, :vault_id, :path, :action, :status, :message,
                    :requested_by, :requested_at, :updated_at, :group_id,
                    :group_path, :total_bytes, :transferred_bytes
                )
                """
            ),
            {
                "id": row["id"],
                "vault_id": row["vault_id"],
                "path": row["path"],
                "action": row["action"],
                "status": row["status"],
                "message": row["message"],
                "requested_by": row["requested_by"],
                "requested_at": row["requested_at"],
                "updated_at": row["updated_at"],
                "group_id": row["group_id"],
                "group_path": row["group_path"],
                "total_bytes": row["total_bytes"],
                "transferred_bytes": row["transferred_bytes"],
            },
        )

    _sync_jobs_sequence(connection)
    op.drop_table("local_copies")
    op.drop_table("delete_markers")
    op.drop_table("archive_versions")
    op.drop_table("file_paths")
    op.drop_table("vault_files")
