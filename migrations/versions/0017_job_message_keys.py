"""Add stable Job message keys for localization.

Revision ID: 0017_job_message_keys
Revises: 0016_ops_observability
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0017_job_message_keys"
down_revision: str | None = "0016_ops_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("message_key", sa.Text()))
        batch.add_column(sa.Column("message_params", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("message_params")
        batch.drop_column("message_key")
