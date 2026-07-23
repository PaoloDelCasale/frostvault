"""Add lifecycle profile JSON to lifecycle policies.

Revision ID: 0012_lifecycle_profile_json
Revises: 0011_lifecycle_policies
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0012_lifecycle_profile_json"
down_revision: str | None = "0011_lifecycle_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("lifecycle_policies") as batch:
        batch.add_column(sa.Column("profile_json", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("lifecycle_policies") as batch:
        batch.drop_column("profile_json")
