"""Add invites and make user passwords optional.

Revision ID: 0005_invites
Revises: 0004_oidc_login
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005_invites"
down_revision: str | None = "0004_oidc_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "invites",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "target_user_id", identifier,
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_by", identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("redeemed_at", sa.Text()),
        sa.Column("redeemed_issuer", sa.Text()),
        sa.Column("redeemed_subject", sa.Text()),
    )
    op.create_index("invites_target_user_idx", "invites", ["target_user_id"])

    # Shell Users (and OIDC-only Users) carry no local password.
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "password_hash", existing_type=sa.Text(), nullable=True
        )
    # SQLite batch recreate cannot reflect the expression-based unique index, so
    # restore the case-insensitive username guard by hand.
    if op.get_bind().dialect.name == "sqlite":
        op.create_index(
            "users_username_lower_uq",
            "users",
            [sa.text("lower(username)")],
            unique=True,
        )

    # The invite_id reserved by 0004 can now reference a real invite.
    with op.batch_alter_table("oidc_login") as batch_op:
        batch_op.create_foreign_key(
            "oidc_login_invite_fk", "invites",
            ["invite_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("oidc_login") as batch_op:
        batch_op.drop_constraint("oidc_login_invite_fk", type_="foreignkey")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "password_hash", existing_type=sa.Text(), nullable=False
        )
    if op.get_bind().dialect.name == "sqlite":
        op.create_index(
            "users_username_lower_uq",
            "users",
            [sa.text("lower(username)")],
            unique=True,
        )
    op.drop_index("invites_target_user_idx", table_name="invites")
    op.drop_table("invites")
