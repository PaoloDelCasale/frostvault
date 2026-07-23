"""Add per-vault operation policies and timestamped cost price books.

Revision ID: 0020_operation_policies
Revises: 0019_cloud_deletion
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0020_operation_policies"
down_revision: str | None = "0019_cloud_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "vault_operation_policies",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "auto_upload",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "stability_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column(
            "include_globs_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "exclude_globs_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("bandwidth_limit_kibps", sa.Integer()),
        sa.Column(
            "operating_windows_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.CheckConstraint(
            "stability_seconds >= 0",
            name="vault_operation_policies_stability_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "bandwidth_limit_kibps IS NULL OR bandwidth_limit_kibps >= 0",
            name="vault_operation_policies_bandwidth_nonnegative_ck",
        ),
    )

    op.create_table(
        "cost_price_books",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="EUR"),
        sa.Column("effective_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("assumptions_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("storage_rates_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("restore_rates_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "cost_price_books_active_idx",
        "cost_price_books",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("cost_price_books_active_idx", table_name="cost_price_books")
    op.drop_table("cost_price_books")
    op.drop_table("vault_operation_policies")
