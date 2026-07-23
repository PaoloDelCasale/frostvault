"""Add upload retry scheduling columns to jobs.

Revision ID: 0013_upload_retry_schedule
Revises: 0012_lifecycle_profile_json
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0013_upload_retry_schedule"
down_revision: str | None = "0012_lifecycle_profile_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column(
                "retry_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("retry_after", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("retry_after")
        batch.drop_column("retry_count")
