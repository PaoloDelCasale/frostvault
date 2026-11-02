"""Keep the Owner inbox and email; remove configurable notification routing.

SMTP is now deployment-owned (#414). Existing inbox rows and email deliveries
are retained; obsolete transport configuration, subscriptions and preferences
are discarded. No catalog, Archive Version, Local Copy or Job is modified.
Downgrade recreates empty configuration tables, not their removed values.

Revision ID: 0046_simplify_notifications
Revises: 0045_drop_quotas_windows
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_simplify_notifications"
down_revision: str | None = "0045_drop_quotas_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM notification_deliveries WHERE channel != 'email'")
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_constraint("notification_deliveries_channel_ck", type_="check")
        batch.create_check_constraint("notification_deliveries_channel_ck", "channel = 'email'")
    for table in (
        "push_subscriptions",
        "user_vault_notification_preferences",
        "vault_notification_preferences",
        "notification_endpoints",
    ):
        op.drop_table(table)


def downgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "notification_endpoints",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default="default"),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('webhook', 'smtp')", name="notification_endpoints_kind_ck"),
    )
    op.create_index("notification_endpoints_kind_uq", "notification_endpoints", ["kind", "name"], unique=True)
    op.create_table(
        "vault_notification_preferences",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("recipient_user_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.CheckConstraint("channel IN ('in_app', 'webhook', 'email')", name="vault_notification_preferences_channel_ck"),
        sa.UniqueConstraint("vault_id", "event", "channel", name="vault_notification_preferences_uq"),
    )
    op.create_table(
        "user_vault_notification_preferences",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("event IN ('job_completed', 'job_failed', 'archive_version_missing')", name="user_vault_notification_preferences_event_ck"),
        sa.CheckConstraint("channel IN ('in_app', 'push')", name="user_vault_notification_preferences_channel_ck"),
        sa.UniqueConstraint("user_id", "vault_id", "event", "channel", name="user_vault_notification_preferences_uq"),
    )
    op.create_index("user_vault_notification_preferences_user_vault_idx", "user_vault_notification_preferences", ["user_id", "vault_id"])
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Text(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("endpoint", name="push_subscriptions_endpoint_uq"),
    )
    op.create_index("push_subscriptions_user_idx", "push_subscriptions", ["user_id"])
    op.create_index("push_subscriptions_session_idx", "push_subscriptions", ["session_id"])
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_constraint("notification_deliveries_channel_ck", type_="check")
        batch.create_check_constraint("notification_deliveries_channel_ck", "channel IN ('webhook', 'email', 'push')")
