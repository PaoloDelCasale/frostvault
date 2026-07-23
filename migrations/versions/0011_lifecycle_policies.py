"""Add lifecycle policy identities and folder overrides.

Revision ID: 0011_lifecycle_policies
Revises: 0010_vault_encryption
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0011_lifecycle_policies"
down_revision: str | None = "0010_vault_encryption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    uuid_type = sa.String(36)
    op.create_table(
        "lifecycle_policies",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "lifecycle_policies_vault_idx",
        "lifecycle_policies",
        ["vault_id"],
    )
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(sa.Column("default_lifecycle_policy_id", uuid_type))
        batch.create_foreign_key(
            "vaults_default_lifecycle_policy_fk",
            "lifecycle_policies",
            ["default_lifecycle_policy_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_table(
        "folder_policy_overrides",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("folder_path", sa.Text(), nullable=False),
        sa.Column(
            "policy_id",
            uuid_type,
            sa.ForeignKey("lifecycle_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("vault_id", "folder_path"),
    )


def downgrade() -> None:
    op.drop_table("folder_policy_overrides")
    with op.batch_alter_table("vaults") as batch:
        batch.drop_constraint("vaults_default_lifecycle_policy_fk", type_="foreignkey")
        batch.drop_column("default_lifecycle_policy_id")
    op.drop_table("lifecycle_policies")
