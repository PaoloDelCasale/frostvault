"""Add immutable Cloud History Policy on Vaults.

Revision ID: 0041_cloud_history_policy
Revises: 0040_storage_class_source
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0041_cloud_history_policy"
down_revision: str | None = "0040_storage_class_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(
            sa.Column(
                "cloud_history_policy",
                sa.Text(),
                nullable=False,
                server_default="archive_history",
            )
        )
        batch.create_check_constraint(
            "vaults_cloud_history_policy_ck",
            "cloud_history_policy IN ('archive_history', 'current_snapshot')",
        )


def downgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.drop_constraint(
            "vaults_cloud_history_policy_ck",
            type_="check",
        )
        batch.drop_column("cloud_history_policy")
