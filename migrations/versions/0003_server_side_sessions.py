"""Add server-side sessions.

Revision ID: 0003_server_side_sessions
Revises: 0002_versioned_archive
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_server_side_sessions"
down_revision: str | None = "0002_versioned_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    uuid_type = sa.String(36)
    op.create_table(
        "sessions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("auth_method", sa.Text(), nullable=False),
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="SET NULL"),
        ),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("idle_expires_at", sa.Text(), nullable=False),
        sa.Column("absolute_expires_at", sa.Text(), nullable=False),
        sa.Column("reauth_at", sa.Text()),
        sa.Column("ip", sa.Text()),
        sa.Column("user_agent", sa.Text()),
        sa.Column("revoked_at", sa.Text()),
        sa.Column("rotated_to", uuid_type),
        sa.CheckConstraint(
            "auth_method IN ('oidc', 'local')",
            name="sessions_auth_method_ck",
        ),
    )
    op.create_index("sessions_user_idx", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("sessions")
