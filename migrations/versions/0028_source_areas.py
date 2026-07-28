"""Add exclusive Source Area grants.

Revision ID: 0028_source_areas
Revises: 0027_cloud_purge_job_bytes
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0028_source_areas"
down_revision: str | None = "0027_cloud_purge_job_bytes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "source_areas",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            identifier,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("volume_alias", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "volume_alias",
            "relative_path",
            name="source_areas_volume_relative_uq",
        ),
    )
    op.create_index(
        "source_areas_user_idx",
        "source_areas",
        ["user_id"],
    )
    op.create_index(
        "source_areas_volume_idx",
        "source_areas",
        ["volume_alias"],
    )


def downgrade() -> None:
    op.drop_index("source_areas_volume_idx", table_name="source_areas")
    op.drop_index("source_areas_user_idx", table_name="source_areas")
    op.drop_table("source_areas")
