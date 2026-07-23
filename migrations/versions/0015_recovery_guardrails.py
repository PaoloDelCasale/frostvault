"""Add recovery approval and restore request metadata to jobs.

Revision ID: 0015_recovery_guardrails
Revises: 0014_rename_jobs
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0015_recovery_guardrails"
down_revision: str | None = "0014_rename_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("restore_tier", sa.Text()))
        batch.add_column(sa.Column("restore_days", sa.Integer()))
        batch.add_column(sa.Column("estimated_cost_eur", sa.Float()))
        batch.add_column(sa.Column("estimated_hours", sa.Float()))
        batch.add_column(sa.Column("pending_until", sa.Text()))
        batch.add_column(sa.Column("approved_by", sa.Integer()))
        batch.add_column(sa.Column("approved_at", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("approved_at")
        batch.drop_column("approved_by")
        batch.drop_column("pending_until")
        batch.drop_column("estimated_hours")
        batch.drop_column("estimated_cost_eur")
        batch.drop_column("restore_days")
        batch.drop_column("restore_tier")
