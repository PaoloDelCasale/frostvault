"""Remove the retired upload transport identity; cloud identities stay intact."""
from alembic import op
import sqlalchemy as sa

revision = "0052_s3_sdk"
down_revision = "0051_plaintext_vaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.drop_column("rclone_remote")


def downgrade() -> None:
    # Downgrade restores structure only. An operator must configure the old
    # transport deliberately; never guess a remote or rewrite cloud identity.
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(sa.Column("rclone_remote", sa.Text(), nullable=False,
                                   server_default=""))
