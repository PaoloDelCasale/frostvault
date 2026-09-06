"""Durable terminal notification intent on Jobs (issue #300).

Revision ID: 0038_job_terminal_notification
Revises: 0037_directory_aggregates
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0038_job_terminal_notification"
down_revision: str | None = "0037_directory_aggregates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column("terminal_notification_status", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("terminal_notification_status")
