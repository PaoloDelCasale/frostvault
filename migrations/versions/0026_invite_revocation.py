"""Allow administrators to revoke a pending Invite.

Revocation is recorded on the Invite itself rather than deleting the row, so
the administrative trail survives and redemption can race against it with a
conditional UPDATE (issue #135).

Revision ID: 0026_invite_revocation
Revises: 0025_oidc_configuration
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0026_invite_revocation"
down_revision: str | None = "0025_oidc_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    with op.batch_alter_table("invites") as batch_op:
        batch_op.add_column(sa.Column("revoked_at", sa.Text()))
        batch_op.add_column(sa.Column("revoked_by", identifier))
        batch_op.create_foreign_key(
            "invites_revoked_by_fk", "users", ["revoked_by"], ["id"],
            ondelete="SET NULL",
        )
    # SQLite recreates the table for a batch alter and cannot reflect the
    # expression-based index restored by 0005, so keep it in place by hand.
    if op.get_bind().dialect.name == "sqlite":
        op.create_index(
            "invites_pending_idx",
            "invites",
            ["expires_at"],
            sqlite_where=sa.text("redeemed_at IS NULL AND revoked_at IS NULL"),
        )
    else:
        op.create_index(
            "invites_pending_idx",
            "invites",
            ["expires_at"],
            postgresql_where=sa.text("redeemed_at IS NULL AND revoked_at IS NULL"),
        )


def downgrade() -> None:
    op.drop_index("invites_pending_idx", table_name="invites")
    with op.batch_alter_table("invites") as batch_op:
        batch_op.drop_constraint("invites_revoked_by_fk", type_="foreignkey")
        batch_op.drop_column("revoked_by")
        batch_op.drop_column("revoked_at")
