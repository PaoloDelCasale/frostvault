"""Persist Vault root identity and relocation recovery state.

Revision ID: 0030_vault_root_relocation
Revises: 0029_source_volume_identity
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_vault_root_relocation"
down_revision: str | None = "0029_source_volume_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(sa.Column("root_identity_version", sa.Text(), nullable=True))
        batch.add_column(sa.Column("root_identity", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "relocation_state",
                sa.Text(),
                nullable=False,
                server_default="ready",
            )
        )
        batch.add_column(sa.Column("relocation_previous_root", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.drop_column("relocation_previous_root")
        batch.drop_column("relocation_state")
        batch.drop_column("root_identity")
        batch.drop_column("root_identity_version")
