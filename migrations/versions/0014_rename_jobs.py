"""Allow rename jobs in the jobs action check.

Revision ID: 0014_rename_jobs
Revises: 0013_upload_retry_schedule
"""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = "0014_rename_jobs"
down_revision: str | None = "0013_upload_retry_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_action_check", type_="check")
        batch.create_check_constraint(
            "jobs_action_check",
            "action IN ('upload', 'recover', 'free-space', 'rename')",
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_action_check", type_="check")
        batch.create_check_constraint(
            "jobs_action_check",
            "action IN ('upload', 'recover', 'free-space')",
        )
