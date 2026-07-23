"""Add per-vault encryption mode and sealed crypt secrets.

Revision ID: 0010_vault_encryption
Revises: 0009_vault_quotas
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0010_vault_encryption"
down_revision: str | None = "0009_vault_quotas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(
            sa.Column(
                "encryption_mode",
                sa.Text(),
                nullable=False,
                server_default="plain",
            )
        )
        batch.add_column(sa.Column("crypt_password_ciphertext", sa.Text()))
        batch.add_column(sa.Column("crypt_password2_ciphertext", sa.Text()))
        batch.add_column(sa.Column("recovery_custody_confirmed_at", sa.Text()))
        batch.create_check_constraint(
            "vaults_encryption_mode_ck",
            "encryption_mode IN ('plain', 'crypt')",
        )
        batch.create_check_constraint(
            "vaults_crypt_secrets_ck",
            "("
            "encryption_mode = 'plain' "
            "AND crypt_password_ciphertext IS NULL "
            "AND crypt_password2_ciphertext IS NULL"
            ") OR ("
            "encryption_mode = 'crypt' "
            "AND crypt_password_ciphertext IS NOT NULL "
            "AND crypt_password2_ciphertext IS NOT NULL"
            ")",
        )


def downgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.drop_constraint("vaults_crypt_secrets_ck", type_="check")
        batch.drop_constraint("vaults_encryption_mode_ck", type_="check")
        batch.drop_column("recovery_custody_confirmed_at")
        batch.drop_column("crypt_password2_ciphertext")
        batch.drop_column("crypt_password_ciphertext")
        batch.drop_column("encryption_mode")
