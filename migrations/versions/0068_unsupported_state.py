"""Add the Unsupported protection state to the derived directory rollups (#524).

Symlinks, FIFOs, sockets and devices used to count as Problem. They now have
their own state, which never counts against the Vault's protection.

Revision ID: 0068_unsupported_state
Revises: 0067_upload_multipart_journal
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_unsupported_state"
down_revision = "0067_upload_multipart_journal"
branch_labels = None
depends_on = None


def _invalidate() -> None:
    # Only the disposable rollups are discarded and rebuilt from the catalog.
    # No Local Copy, Archive Version, Job, integrity/availability evidence or
    # cloud content is changed.
    op.execute("DELETE FROM directory_aggregates")
    op.execute("""
        INSERT INTO directory_aggregate_status(vault_id, status, updated_at)
        SELECT id, 'rebuild_required', CURRENT_TIMESTAMP::text FROM vaults
        ON CONFLICT(vault_id) DO UPDATE SET status='rebuild_required',
            updated_at=excluded.updated_at
    """)


def upgrade() -> None:
    _invalidate()
    op.add_column("directory_aggregates", sa.Column("state_unsupported", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    _invalidate()
    op.drop_column("directory_aggregates", "state_unsupported")
