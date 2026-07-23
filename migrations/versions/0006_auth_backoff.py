"""Add authentication backoff counters.

Revision ID: 0006_auth_backoff
Revises: 0005_invites
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_auth_backoff"
down_revision: str | None = "0005_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "auth_backoff",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "failure_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("next_allowed_at", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("scope", "key", name="auth_backoff_scope_key_uq"),
        sa.CheckConstraint(
            "scope IN ('ip', 'account')", name="auth_backoff_scope_ck"
        ),
    )


def downgrade() -> None:
    op.drop_table("auth_backoff")
