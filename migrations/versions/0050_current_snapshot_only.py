"""Current Snapshot is the only cloud behavior (#418).

Revision ID: 0050_current_snapshot_only
Revises: 0049_essential_file_actions

Schema only: no cloud calls and no changes to Archive Version trust or content.
Downgrade cannot restore a retired policy choice; every Vault remains snapshot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_current_snapshot_only"
down_revision = "0049_essential_file_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.drop_constraint("vaults_cloud_history_policy_ck", type_="check")
        batch.drop_column("cloud_history_policy")


def downgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(sa.Column(
            "cloud_history_policy", sa.Text(), nullable=False,
            server_default="current_snapshot",
        ))
        batch.create_check_constraint(
            "vaults_cloud_history_policy_ck",
            "cloud_history_policy IN ('archive_history', 'current_snapshot')",
        )
