"""Durable missing-archive incidents and per-component scan health (issue #303).

Revision ID: 0039_catalog_incidents
Revises: 0038_job_terminal_notification
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0039_catalog_incidents"
down_revision: str | None = "0038_job_terminal_notification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    uuid_type = sa.String(36)

    op.create_table(
        "catalog_incidents",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vault_file_id", uuid_type, nullable=False),
        sa.Column("archive_version_id", uuid_type, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text()),
        sa.Column("provider_version_id", sa.Text()),
        sa.Column("opened_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.Text()),
        sa.CheckConstraint(
            "kind IN ('archive_version_missing')",
            name="catalog_incidents_kind_ck",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="catalog_incidents_status_ck",
        ),
    )
    op.create_index(
        "catalog_incidents_vault_status_idx",
        "catalog_incidents",
        ["vault_id", "status"],
    )
    open_status = sa.text("status = 'open'")
    op.create_index(
        "catalog_incidents_one_open_version_uq",
        "catalog_incidents",
        ["archive_version_id", "kind"],
        unique=True,
        sqlite_where=open_status,
        postgresql_where=open_status,
    )

    op.create_table(
        "vault_component_health",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("component", sa.Text(), primary_key=True),
        sa.Column("last_success_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_verified_at", sa.Text()),
        sa.Column("healthy", sa.Boolean()),
        sa.Column("extra_json", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "component IN ('source', 'cloud', 'policy', 'audit')",
            name="vault_component_health_component_ck",
        ),
    )

    with op.batch_alter_table("user_vault_notification_preferences") as batch:
        batch.drop_constraint(
            "user_vault_notification_preferences_event_ck",
            type_="check",
        )
        batch.create_check_constraint(
            "user_vault_notification_preferences_event_ck",
            "event IN ('job_completed', 'job_failed', 'archive_version_missing')",
        )


def downgrade() -> None:
    with op.batch_alter_table("user_vault_notification_preferences") as batch:
        batch.drop_constraint(
            "user_vault_notification_preferences_event_ck",
            type_="check",
        )
        batch.create_check_constraint(
            "user_vault_notification_preferences_event_ck",
            "event IN ('job_completed', 'job_failed')",
        )
    op.drop_table("vault_component_health")
    op.drop_index(
        "catalog_incidents_one_open_version_uq", table_name="catalog_incidents"
    )
    op.drop_index(
        "catalog_incidents_vault_status_idx", table_name="catalog_incidents"
    )
    op.drop_table("catalog_incidents")
