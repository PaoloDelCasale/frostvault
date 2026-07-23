"""Add nullable per-vault quota limits.

Revision ID: 0009_vault_quotas
Revises: 0008_lookup_rate_limit
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0009_vault_quotas"
down_revision: str | None = "0008_lookup_rate_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "vault_quotas",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("storage_soft_limit_bytes", sa.BigInteger()),
        sa.Column("storage_hard_limit_bytes", sa.BigInteger()),
        sa.Column("concurrency_soft_limit", sa.Integer()),
        sa.Column("concurrency_hard_limit", sa.Integer()),
        sa.Column("restore_30d_soft_limit_bytes", sa.BigInteger()),
        sa.Column("restore_30d_hard_limit_bytes", sa.BigInteger()),
        sa.CheckConstraint(
            "storage_soft_limit_bytes IS NULL OR storage_soft_limit_bytes >= 0",
            name="vault_quotas_storage_soft_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "storage_hard_limit_bytes IS NULL OR storage_hard_limit_bytes >= 0",
            name="vault_quotas_storage_hard_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "concurrency_soft_limit IS NULL OR concurrency_soft_limit >= 0",
            name="vault_quotas_concurrency_soft_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "concurrency_hard_limit IS NULL OR concurrency_hard_limit >= 0",
            name="vault_quotas_concurrency_hard_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "restore_30d_soft_limit_bytes IS NULL OR restore_30d_soft_limit_bytes >= 0",
            name="vault_quotas_restore_soft_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "restore_30d_hard_limit_bytes IS NULL OR restore_30d_hard_limit_bytes >= 0",
            name="vault_quotas_restore_hard_nonnegative_ck",
        ),
        sa.CheckConstraint(
            "storage_soft_limit_bytes IS NULL OR storage_hard_limit_bytes IS NULL "
            "OR storage_soft_limit_bytes <= storage_hard_limit_bytes",
            name="vault_quotas_storage_order_ck",
        ),
        sa.CheckConstraint(
            "concurrency_soft_limit IS NULL OR concurrency_hard_limit IS NULL "
            "OR concurrency_soft_limit <= concurrency_hard_limit",
            name="vault_quotas_concurrency_order_ck",
        ),
        sa.CheckConstraint(
            "restore_30d_soft_limit_bytes IS NULL OR restore_30d_hard_limit_bytes IS NULL "
            "OR restore_30d_soft_limit_bytes <= restore_30d_hard_limit_bytes",
            name="vault_quotas_restore_order_ck",
        ),
    )


def downgrade() -> None:
    op.drop_table("vault_quotas")
