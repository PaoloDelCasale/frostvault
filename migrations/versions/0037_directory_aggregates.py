"""Durable directory aggregates for scaled browsing (issue #229).

Revision ID: 0037_directory_aggregates
Revises: 0036_catalog_events

Derived projection only. Canonical Vault File / Archive Version rows are
unchanged. Status starts as rebuild_required so the application rebuilds each
Vault lazily (or via explicit flush) instead of scanning huge catalogs inside
the migration transaction.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0037_directory_aggregates"
down_revision: str | None = "0036_catalog_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "directory_aggregate_status",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="rebuild_required"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ready', 'rebuild_required')",
            name="directory_aggregate_status_ck",
        ),
    )

    # Durable dirty-directory set so a crashed connection still converges on
    # the next listing/revision flush instead of losing in-memory marks.
    op.create_table(
        "directory_aggregate_dirty",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("marked_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "directory_aggregate_dirty_vault_idx",
        "directory_aggregate_dirty",
        ["vault_id"],
    )

    op.create_table(
        "directory_aggregates",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("parent_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("item_count", identifier, nullable=False, server_default="0"),
        sa.Column("total_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("local_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cloud_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state_local_only", identifier, nullable=False, server_default="0"),
        sa.Column("state_cloud_only", identifier, nullable=False, server_default="0"),
        sa.Column("state_both", identifier, nullable=False, server_default="0"),
        sa.Column("state_restoring", identifier, nullable=False, server_default="0"),
        sa.Column("action_upload", identifier, nullable=False, server_default="0"),
        sa.Column("action_recover", identifier, nullable=False, server_default="0"),
        sa.Column("action_free_space", identifier, nullable=False, server_default="0"),
        sa.Column("action_cloud_archive", identifier, nullable=False, server_default="0"),
        sa.Column("action_cloud_purge", identifier, nullable=False, server_default="0"),
        sa.Column("action_storage_class", identifier, nullable=False, server_default="0"),
        sa.Column("pinned_count", identifier, nullable=False, server_default="0"),
        sa.Column(
            "storage_class_counts",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("item_count >= 0", name="directory_aggregates_item_count_ck"),
        sa.CheckConstraint("total_size >= 0", name="directory_aggregates_total_size_ck"),
        sa.CheckConstraint("local_size >= 0", name="directory_aggregates_local_size_ck"),
        sa.CheckConstraint("cloud_size >= 0", name="directory_aggregates_cloud_size_ck"),
        sa.CheckConstraint(
            "pinned_count >= 0 AND pinned_count <= item_count",
            name="directory_aggregates_pinned_ck",
        ),
    )
    op.create_index(
        "directory_aggregates_parent_name_idx",
        "directory_aggregates",
        ["vault_id", "parent_path", "name"],
    )

    # Seed rebuild markers for existing Vaults. Application code rebuilds lazily.
    op.execute(
        sa.text(
            """
            INSERT INTO directory_aggregate_status(vault_id, status, updated_at)
            SELECT id, 'rebuild_required', '1970-01-01T00:00:00+00:00'
            FROM vaults
            """
        )
    )

    # Help direct-file page queries under a directory prefix.
    op.create_index(
        "file_paths_vault_current_path_idx",
        "file_paths",
        ["vault_id", "path"],
        sqlite_where=sa.text("valid_to IS NULL"),
        postgresql_where=sa.text("valid_to IS NULL"),
    )


def _table_has_rows(table: str) -> bool:
    connection = op.get_bind()
    row = connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
    return row is not None


def downgrade() -> None:
    # Keep head pinned when durable catalog history or derived directory
    # aggregates exist so a multi-step downgrade cannot stop halfway at 0036
    # after silently dropping the projection.
    durable_tables = (
        "directory_aggregates",
        "directory_aggregate_dirty",
        "catalog_events",
        "vault_catalog_revisions",
    )
    if any(_table_has_rows(table) for table in durable_tables):
        raise RuntimeError(
            "Cannot downgrade 0037 after catalog event or directory aggregate "
            "data has been persisted; export or explicitly clear derived "
            "history first"
        )

    op.drop_index("file_paths_vault_current_path_idx", table_name="file_paths")
    op.drop_index(
        "directory_aggregates_parent_name_idx",
        table_name="directory_aggregates",
    )
    op.drop_table("directory_aggregates")
    op.drop_index(
        "directory_aggregate_dirty_vault_idx",
        table_name="directory_aggregate_dirty",
    )
    op.drop_table("directory_aggregate_dirty")
    op.drop_table("directory_aggregate_status")
