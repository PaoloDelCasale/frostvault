"""Persist owner username lookup rate-limit attempts.

Revision ID: 0008_lookup_rate_limit
Revises: 0007_vault_ownership
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008_lookup_rate_limit"
down_revision: str | None = "0007_vault_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "lookup_rate_limit_keys",
        sa.Column(
            "user_id",
            identifier,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("client_ip", sa.Text(), primary_key=True),
    )
    op.create_table(
        "lookup_rate_limit_attempts",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            identifier,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_ip", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "lookup_rate_limit_attempts_key_time_idx",
        "lookup_rate_limit_attempts",
        ["user_id", "client_ip", "attempted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "lookup_rate_limit_attempts_key_time_idx",
        table_name="lookup_rate_limit_attempts",
    )
    op.drop_table("lookup_rate_limit_attempts")
    op.drop_table("lookup_rate_limit_keys")
