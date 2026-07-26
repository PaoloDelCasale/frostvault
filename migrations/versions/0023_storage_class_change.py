"""Allow storage-class change Jobs and lifecycle pins (issue #110).

Revision ID: 0023_storage_class_change
Revises: 0022_push_subscriptions
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0023_storage_class_change"
down_revision: str | None = "0022_push_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_action_check", type_="check")
        batch.create_check_constraint(
            "jobs_action_check",
            "action IN ("
            "'upload', 'recover', 'free-space', 'rename', "
            "'cloud-archive', 'cloud-purge', 'storage-class'"
            ")",
        )
        batch.add_column(sa.Column("target_storage_class", sa.Text()))

    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "lifecycle_pins",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("is_directory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pinned_at", sa.Text(), nullable=False),
        sa.Column(
            "pinned_by",
            identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.PrimaryKeyConstraint("vault_id", "path", name="lifecycle_pins_pk"),
    )
    op.create_index(
        "lifecycle_pins_vault_idx",
        "lifecycle_pins",
        ["vault_id"],
    )


def downgrade() -> None:
    op.drop_index("lifecycle_pins_vault_idx", table_name="lifecycle_pins")
    op.drop_table("lifecycle_pins")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("target_storage_class")
        batch.drop_constraint("jobs_action_check", type_="check")
        batch.create_check_constraint(
            "jobs_action_check",
            "action IN ("
            "'upload', 'recover', 'free-space', 'rename', "
            "'cloud-archive', 'cloud-purge'"
            ")",
        )
