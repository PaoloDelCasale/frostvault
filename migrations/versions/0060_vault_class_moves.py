"""Durable exact-version Vault-wide class-copy intents (#429).

Metadata only; no provider calls or changes to existing trust.
"""
from alembic import op
import sqlalchemy as sa

revision = "0060_vault_class_moves"
down_revision = "0059_delayed_cloud_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vault_class_moves",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("token", sa.String(36), nullable=False, unique=True),
        sa.Column("source_archive_id", sa.String(36), sa.ForeignKey("archive_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_version_id", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("destination_version_id", sa.Text()),
        sa.Column("composite_checksum", sa.Text()),
        sa.Column("destination_archive_id", sa.String(36), sa.ForeignKey("archive_versions.id", ondelete="RESTRICT")),
        sa.Column("copy_started", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("size >= 0", name="vault_class_moves_size_ck"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("""SELECT 1 FROM vault_class_moves m JOIN jobs j ON j.id=m.job_id
        WHERE j.status NOT IN ('completed','cancelled') LIMIT 1""")).first():
        raise RuntimeError("Complete or cancel Vault moves before downgrading; preserve the copy ledger")
    op.drop_table("vault_class_moves")
