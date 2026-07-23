"""Cloud deletion setting, purge delay jobs, and resumable purge items.

Revision ID: 0019_cloud_deletion
Revises: 0018_metadata_backups
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0019_cloud_deletion"
down_revision: str | None = "0018_metadata_backups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(
            sa.Column(
                "cloud_deletion_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_action_check", type_="check")
        batch.create_check_constraint(
            "jobs_action_check",
            "action IN ("
            "'upload', 'recover', 'free-space', 'rename', "
            "'cloud-archive', 'cloud-purge'"
            ")",
        )
        batch.add_column(sa.Column("reason", sa.Text()))
        batch.add_column(sa.Column("confirmation_phrase", sa.Text()))

    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "cloud_deletion_items",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            identifier,
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vault_id", identifier, sa.ForeignKey("vaults.id"), nullable=False),
        sa.Column("vault_file_id", sa.Text(), sa.ForeignKey("vault_files.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("archive_version_id", sa.Text()),
        sa.Column("delete_marker_id", sa.Text()),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("provider_version_id", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text()),
        sa.Column("updated_at", sa.Text()),
        sa.CheckConstraint(
            "kind IN ('version', 'delete_marker')",
            name="cloud_deletion_items_kind_ck",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'deleted', 'failed', 'skipped')",
            name="cloud_deletion_items_status_ck",
        ),
    )
    op.create_index(
        "cloud_deletion_items_job_status_idx",
        "cloud_deletion_items",
        ["job_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("cloud_deletion_items_job_status_idx", table_name="cloud_deletion_items")
    op.drop_table("cloud_deletion_items")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("confirmation_phrase")
        batch.drop_column("reason")
        batch.drop_constraint("jobs_action_check", type_="check")
        batch.create_check_constraint(
            "jobs_action_check",
            "action IN ('upload', 'recover', 'free-space', 'rename')",
        )
    with op.batch_alter_table("vaults") as batch:
        batch.drop_column("cloud_deletion_enabled")
