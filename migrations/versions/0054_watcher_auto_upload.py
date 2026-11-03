"""Persist content stability and enable automatic protection for new Vaults.

Revision ID: 0054_watcher_auto_upload
Revises: 0053_vault_storage_plans
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_watcher_auto_upload"
down_revision = "0053_vault_storage_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Missing policy rows used to mean manual uploads. Materialize that choice
    # before changing the product default; never silently unpause an old Vault.
    op.execute("""
        INSERT INTO vault_operation_policies(vault_id, auto_upload)
        SELECT id, FALSE FROM vaults
        WHERE NOT EXISTS (
            SELECT 1 FROM vault_operation_policies p WHERE p.vault_id=vaults.id
        )
    """)
    op.alter_column("vault_operation_policies", "auto_upload", server_default=sa.true())
    # Null legacy observations fall back to observed_at, not an invented age.
    op.add_column("local_copies", sa.Column("content_observed_at", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("local_copies", "content_observed_at")
    op.alter_column("vault_operation_policies", "auto_upload", server_default=sa.false())
    # Keep explicit choices, including the migration's preserved pauses.
