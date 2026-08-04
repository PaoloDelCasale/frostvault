"""Add per-user Vault notification preferences and keyed inbox fields.

Revision ID: 0032_notification_inbox
Revises: 0031_vault_decommission
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0032_notification_inbox"
down_revision: str | None = "0031_vault_decommission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "user_vault_notification_preferences",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            identifier,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            "event IN ('job_completed', 'job_failed')",
            name="user_vault_notification_preferences_event_ck",
        ),
        sa.CheckConstraint(
            "channel IN ('in_app', 'push')",
            name="user_vault_notification_preferences_channel_ck",
        ),
        sa.UniqueConstraint(
            "user_id",
            "vault_id",
            "event",
            "channel",
            name="user_vault_notification_preferences_uq",
        ),
    )
    op.create_index(
        "user_vault_notification_preferences_user_vault_idx",
        "user_vault_notification_preferences",
        ["user_id", "vault_id"],
    )

    # Keep all existing notifications readable.  In particular, the keyed
    # fields are nullable so historical rows retain their stored title/body.
    with op.batch_alter_table("notifications") as batch:
        batch.add_column(sa.Column("title_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("body_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("message_params", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "in_app_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(sa.Column("dedupe_key", sa.Text(), nullable=True))

    # NULL dedupe keys intentionally remain allowed for legacy and ad-hoc
    # notifications.  Both SQLite and PostgreSQL permit multiple NULL values
    # in this unique index while making terminal Job notifications idempotent.
    op.create_index(
        "notifications_user_dedupe_uq",
        "notifications",
        ["user_id", "dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("notifications_user_dedupe_uq", table_name="notifications")
    with op.batch_alter_table("notifications") as batch:
        batch.drop_column("dedupe_key")
        batch.drop_column("in_app_enabled")
        batch.drop_column("message_params")
        batch.drop_column("body_key")
        batch.drop_column("title_key")

    op.drop_index(
        "user_vault_notification_preferences_user_vault_idx",
        table_name="user_vault_notification_preferences",
    )
    op.drop_table("user_vault_notification_preferences")
