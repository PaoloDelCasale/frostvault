"""Per-Vault accepted mount and installation-wide recovery custody (#427).

No filesystem/S3 operations and no inherited custody confirmation.
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_reconnect_recovery_kit"
down_revision = "0057_vault_local_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vaults", sa.Column("reconnect_volume_fingerprint", sa.Text(), nullable=True))
    op.create_table("recovery_kit_custody",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("fingerprint", sa.Text(), nullable=False),
                    sa.Column("acknowledged_at", sa.Text(), nullable=False),
                    sa.CheckConstraint("id=1", name="recovery_kit_singleton_ck"))
    op.create_table("metadata_restore_state",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("backup_created_at", sa.Text(), nullable=False),
                    sa.Column("recorded_at", sa.Text(), nullable=False),
                    sa.CheckConstraint("id=1", name="metadata_restore_singleton_ck"))
    op.create_table("metadata_restore_updates",
                    sa.Column("vault_file_id", sa.String(), primary_key=True),
                    sa.Column("observed_at", sa.Text(), nullable=False))


def downgrade() -> None:
    # Old code cannot honor a per-Vault accepted replacement. Fail closed.
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM vaults WHERE reconnect_volume_fingerprint IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Reconnect original Source Volumes before downgrading")
    op.drop_table("metadata_restore_updates")
    op.drop_table("metadata_restore_state")
    op.drop_table("recovery_kit_custody")
    op.drop_column("vaults", "reconnect_volume_fingerprint")
