"""Track metadata backup runs for operator status (issue #15).

Revision ID: 0018_metadata_backups
Revises: 0017_job_message_keys
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0018_metadata_backups"
down_revision: str | None = "0017_job_message_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "metadata_backup_runs",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("digest_sha256", sa.Text()),
        sa.Column("database_sha256", sa.Text()),
        sa.Column("local_path", sa.Text()),
        sa.Column("s3_key", sa.Text()),
        sa.Column("size_bytes", identifier),
        sa.Column("error_message", sa.Text()),
        sa.Column("verified_at", sa.Text()),
        sa.CheckConstraint(
            "reason IN ('scheduled', 'pre_upgrade', 'manual', 'verify')",
            name="metadata_backup_runs_reason_ck",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'verified')",
            name="metadata_backup_runs_status_ck",
        ),
    )
    op.create_index(
        "metadata_backup_runs_created_idx",
        "metadata_backup_runs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("metadata_backup_runs_created_idx", table_name="metadata_backup_runs")
    op.drop_table("metadata_backup_runs")
