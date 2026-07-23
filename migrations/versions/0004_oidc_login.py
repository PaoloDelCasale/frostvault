"""Add OIDC login transient state and external identities.

Revision ID: 0004_oidc_login
Revises: 0003_server_side_sessions
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_oidc_login"
down_revision: str | None = "0003_server_side_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    uuid_type = sa.String(36)
    op.create_table(
        "oidc_login",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("state", sa.Text(), nullable=False, unique=True),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text()),
        # Reserved for PR3 (invite binding); no foreign key until invites exist.
        sa.Column("invite_id", identifier),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
    )
    op.create_index("oidc_login_expires_idx", "oidc_login", ["expires_at"])
    op.create_table(
        "user_identities",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="user_identities_issuer_subject_uq"),
    )
    op.create_index("user_identities_user_idx", "user_identities", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_identities")
    op.drop_table("oidc_login")
