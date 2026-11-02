"""Drop Vault quotas, operating windows, per-Vault bandwidth, and price books.

FrostVault keeps one optional global upload bandwidth limit
(``BANDWIDTH_LIMIT_KIBPS``) and estimates costs from bundled AWS list prices
(#413, #366 Q3/Q6). This removes the per-Vault quota table, the operating
windows and per-Vault bandwidth limit of operation policies, and the
operator-managed cost price books.

Existing data has no compatibility obligation (#362 D6): the downgrade
recreates the empty structures without the removed values.

Revision ID: 0045_drop_quotas_windows
Revises: 0044_single_owner
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0045_drop_quotas_windows"
down_revision: str | None = "0044_single_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identifier() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.drop_table("vault_quotas")
    op.drop_index("cost_price_books_active_idx", table_name="cost_price_books")
    op.drop_table("cost_price_books")
    with op.batch_alter_table("vault_operation_policies") as batch:
        batch.drop_constraint(
            "vault_operation_policies_bandwidth_nonnegative_ck",
            type_="check",
        )
        batch.drop_column("bandwidth_limit_kibps")
        batch.drop_column("operating_windows_json")


def downgrade() -> None:
    with op.batch_alter_table("vault_operation_policies") as batch:
        batch.add_column(sa.Column("bandwidth_limit_kibps", sa.Integer()))
        batch.add_column(
            sa.Column(
                "operating_windows_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )
        batch.create_check_constraint(
            "vault_operation_policies_bandwidth_nonnegative_ck",
            "bandwidth_limit_kibps IS NULL OR bandwidth_limit_kibps >= 0",
        )

    identifier = _identifier()
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
