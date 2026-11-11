"""Persistent manual Vault hold, independent of lifecycle (#537)."""
from alembic import op
import sqlalchemy as sa

revision = "0070_vault_pause"
down_revision = "0069_upload_intent_per_attempt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vaults", sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM vaults WHERE paused LIMIT 1")).first():
        raise RuntimeError("Resume paused Vaults before downgrade; refusing to silently restart cloud work")
    op.drop_column("vaults", "paused")
