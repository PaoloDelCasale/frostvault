"""Replace the derived presence projection with five protection states.

Revision ID: 0055_protection_states
Revises: 0054_watcher_auto_upload
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_protection_states"
down_revision = "0054_watcher_auto_upload"
branch_labels = None
depends_on = None


def _invalidate() -> None:
    # Only disposable projections are discarded. No Local Copy, Archive Version,
    # Job, integrity/availability evidence or cloud content is changed.
    op.execute("DELETE FROM directory_aggregates")
    op.execute("""
        INSERT INTO directory_aggregate_status(vault_id, status, updated_at)
        SELECT id, 'rebuild_required', CURRENT_TIMESTAMP::text FROM vaults
        ON CONFLICT(vault_id) DO UPDATE SET status='rebuild_required',
            updated_at=excluded.updated_at
    """)


def upgrade() -> None:
    _invalidate()
    for old, new in (("local_only", "pending"), ("both", "protected"), ("restoring", "problem")):
        op.alter_column("directory_aggregates", f"state_{old}", new_column_name=f"state_{new}")
    op.add_column("directory_aggregates", sa.Column("state_unconfirmed", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    _invalidate()
    op.drop_column("directory_aggregates", "state_unconfirmed")
    for old, new in (("pending", "local_only"), ("protected", "both"), ("problem", "restoring")):
        op.alter_column("directory_aggregates", f"state_{old}", new_column_name=f"state_{new}")
