"""Web Push subscriptions for Job completion (issue #72).

Revision ID: 0022_push_subscriptions
Revises: 0021_local_retention
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0022_push_subscriptions"
down_revision: str | None = "0021_local_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("endpoint", name="push_subscriptions_endpoint_uq"),
    )
    op.create_index(
        "push_subscriptions_user_idx",
        "push_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "push_subscriptions_session_idx",
        "push_subscriptions",
        ["session_id"],
    )

    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_constraint("notification_deliveries_channel_ck", type_="check")
        batch.create_check_constraint(
            "notification_deliveries_channel_ck",
            "channel IN ('webhook', 'email', 'push')",
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_constraint("notification_deliveries_channel_ck", type_="check")
        batch.create_check_constraint(
            "notification_deliveries_channel_ck",
            "channel IN ('webhook', 'email')",
        )
    op.drop_index("push_subscriptions_session_idx", table_name="push_subscriptions")
    op.drop_index("push_subscriptions_user_idx", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
