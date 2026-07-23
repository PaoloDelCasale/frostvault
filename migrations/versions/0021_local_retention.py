"""Add opt-in Local Copy retention to operation policies.

Revision ID: 0021_local_retention
Revises: 0020_operation_policies
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0021_local_retention"
down_revision: str | None = "0020_operation_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vault_operation_policies") as batch:
        batch.add_column(
            sa.Column(
                "auto_local_cleanup",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("local_retention_days", sa.Integer()))
        batch.create_check_constraint(
            "vault_operation_policies_retention_positive_ck",
            "local_retention_days IS NULL OR local_retention_days > 0",
        )
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column(
                "origin",
                sa.Text(),
                nullable=False,
                server_default="manual",
            )
        )
        batch.create_check_constraint(
            "jobs_origin_ck",
            "origin IN ('manual', 'automatic')",
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_origin_ck", type_="check")
        batch.drop_column("origin")
    with op.batch_alter_table("vault_operation_policies") as batch:
        batch.drop_constraint(
            "vault_operation_policies_retention_positive_ck",
            type_="check",
        )
        batch.drop_column("local_retention_days")
        batch.drop_column("auto_local_cleanup")
