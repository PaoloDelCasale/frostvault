"""Durable upload facts and exact-version replacement authority (metadata only)."""
from alembic import op
import sqlalchemy as sa

revision = "0064_upload_provenance"
down_revision = "0063_upload_verification"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("upload_reconcile_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("snapshot_reconciliation_blocks",
        sa.Column("vault_file_id", sa.Text(), sa.ForeignKey("vault_files.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False))
    op.create_table(
        "upload_intents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("token", sa.Text(), nullable=False, unique=True),
        sa.Column("vault_id", sa.BigInteger(), sa.ForeignKey("vaults.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("vault_file_id", sa.Text(), sa.ForeignKey("vault_files.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("original_job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("target_json", sa.Text(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("requested", sa.Text(), nullable=False),
        sa.Column("inventory_json", sa.Text(), nullable=False),
        sa.Column("send_started", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archive_version_id", sa.Text(), sa.ForeignKey("archive_versions.id", ondelete="RESTRICT")),
        sa.Column("settled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("requested IN ('s3_checksum','read_back')", name="upload_intent_mode"),
    )
    op.create_index("upload_intents_outstanding_file_idx", "upload_intents", ["vault_file_id"], postgresql_where=sa.text("NOT settled"))
    op.create_table(
        "upload_intent_jobs",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("intent_id", sa.Text(), sa.ForeignKey("upload_intents.id", ondelete="RESTRICT"), nullable=False),
    )
    op.create_table(
        "upload_receipts",
        sa.Column("intent_id", sa.Text(), sa.ForeignKey("upload_intents.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("receipt_hash", sa.Text(), primary_key=True),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("received_at", sa.Text(), nullable=False),
    )
    op.create_table("snapshot_cleanup_inventories",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("target_json", sa.Text(), nullable=False),
        sa.Column("inventory_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False))
    # A proven historical duplicate is catalogued here, not elected by AV numbering.
    # Authority pins exact target + keeper + independent digest, never an origin string.
    op.create_table(
        "snapshot_cleanup_proofs",
        sa.Column("vault_file_id", sa.Text(), sa.ForeignKey("vault_files.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("bucket", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("object_key", sa.Text(), primary_key=True),
        sa.Column("version_id", sa.Text(), primary_key=True),
        sa.Column("keeper_id", sa.Text(), sa.ForeignKey("archive_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("proved_at", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.Text()),
        sa.CheckConstraint("method IN ('exact_get_duplicate','independent_lineage')", name="snapshot_cleanup_proof_method"),
    )


def downgrade():
    # No provider query can establish absence during DDL. Even cancelled ambiguous
    # sends retain evidence: refusing first prevents a partial destructive downgrade.
    pending = op.get_bind().execute(sa.text("SELECT 1 FROM upload_intents WHERE send_started AND NOT settled LIMIT 1")).first()
    proofs = op.get_bind().execute(sa.text("SELECT 1 FROM snapshot_cleanup_proofs WHERE deleted_at IS NULL LIMIT 1")).first()
    if pending or proofs:
        raise RuntimeError("Reconcile retained upload intents/cleanup authority with this release before downgrade")
    for table in ("snapshot_cleanup_proofs", "snapshot_cleanup_inventories", "upload_receipts", "upload_intent_jobs", "upload_intents", "snapshot_reconciliation_blocks"):
        op.drop_table(table)
    op.drop_column("jobs", "upload_reconcile_only")
