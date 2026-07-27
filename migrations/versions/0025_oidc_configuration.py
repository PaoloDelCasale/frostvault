"""Add the secure OIDC configuration lifecycle store.

Revision ID: 0025_oidc_configuration
Revises: 0024_system_settings
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0025_oidc_configuration"
down_revision: str | None = "0024_system_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "oidc_configuration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_enabled", sa.Boolean(), nullable=False),
        sa.Column("active_version", sa.Integer()),
        sa.Column("active_issuer", sa.Text()),
        sa.Column("active_client_id", sa.Text()),
        sa.Column("active_secret_ciphertext", sa.Text()),
        sa.Column("active_scopes", sa.JSON()),
        sa.Column("active_login_ttl_seconds", sa.Integer()),
        sa.Column("draft_version", sa.Integer()),
        sa.Column("draft_issuer", sa.Text()),
        sa.Column("draft_client_id", sa.Text()),
        sa.Column("draft_secret_ciphertext", sa.Text()),
        sa.Column("draft_scopes", sa.JSON()),
        sa.Column("draft_login_ttl_seconds", sa.Integer()),
        sa.Column("validated_draft_version", sa.Integer()),
        sa.Column("validation_status", sa.Text()),
        sa.Column("validation_error", sa.Text()),
        sa.Column("validated_at", sa.Text()),
        sa.Column(
            "updated_by",
            identifier,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="oidc_configuration_singleton_ck"),
        sa.CheckConstraint(
            "validation_status IS NULL OR validation_status IN "
            "('not_validated', 'valid', 'invalid')",
            name="oidc_configuration_validation_status_ck",
        ),
    )


def downgrade() -> None:
    op.drop_table("oidc_configuration")
