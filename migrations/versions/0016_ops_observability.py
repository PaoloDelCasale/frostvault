"""Add append-only audit events for operational observability.

Revision ID: 0016_ops_observability
Revises: 0015_recovery_guardrails
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0016_ops_observability"
down_revision: str | None = "0015_recovery_guardrails"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "audit_events",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text()),
        sa.Column(
            "actor_user_id",
            identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "job_id",
            identifier,
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("correlation_id", sa.Text()),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="vault"),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "visibility IN ('vault', 'admin', 'owner')",
            name="audit_events_visibility_ck",
        ),
    )
    op.create_index(
        "audit_events_vault_created_idx",
        "audit_events",
        ["vault_id", "created_at"],
    )
    op.create_index(
        "audit_events_created_idx",
        "audit_events",
        ["created_at"],
    )

    op.create_table(
        "notification_endpoints",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default="default"),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('webhook', 'smtp')",
            name="notification_endpoints_kind_ck",
        ),
    )
    op.create_index(
        "notification_endpoints_kind_uq",
        "notification_endpoints",
        ["kind", "name"],
        unique=True,
    )

    op.create_table(
        "vault_notification_preferences",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("recipient_user_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.CheckConstraint(
            "channel IN ('in_app', 'webhook', 'email')",
            name="vault_notification_preferences_channel_ck",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "event",
            "channel",
            name="vault_notification_preferences_uq",
        ),
    )

    op.create_table(
        "notifications",
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
            sa.ForeignKey("vaults.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "job_id",
            identifier,
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("read_at", sa.Text()),
    )
    op.create_index(
        "notifications_user_created_idx",
        "notifications",
        ["user_id", "created_at"],
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "notification_id",
            identifier,
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "channel IN ('webhook', 'email')",
            name="notification_deliveries_channel_ck",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="notification_deliveries_status_ck",
        ),
    )
    op.create_index(
        "notification_deliveries_pending_idx",
        "notification_deliveries",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "worker_errors",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="SET NULL"),
        ),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "classification IN ("
            "'timeout', 'connectivity', 'permission', 'configuration', "
            "'unexpected')",
            name="worker_errors_classification_ck",
        ),
    )
    op.create_index(
        "worker_errors_created_idx",
        "worker_errors",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("worker_errors")
    op.drop_table("notification_deliveries")
    op.drop_table("notifications")
    op.drop_table("vault_notification_preferences")
    op.drop_table("notification_endpoints")
    op.drop_table("audit_events")
