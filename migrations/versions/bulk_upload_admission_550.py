"""Durable manual bulk upload admission (#550), metadata only."""
from alembic import op
import sqlalchemy as sa

revision = "bulk_upload_admission_550"
down_revision = "0070_vault_pause"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bulk_upload_admissions",
        sa.Column("group_id", sa.String(36), primary_key=True),
        sa.Column("vault_id", sa.BigInteger(), sa.ForeignKey("vaults.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text()),
        sa.Column("origin", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("verification_mode", sa.Text(), nullable=False),
        sa.Column("vault_identity_json", sa.Text(), nullable=False),
        sa.Column("watermark", sa.BigInteger(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("selected_count", sa.BigInteger(), nullable=False),
        sa.Column("processed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("admitted_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("status IN ('pending','completed','cancelled','invalidated')", name="bulk_upload_status_ck"),
        sa.CheckConstraint("origin='manual'", name="bulk_upload_origin_ck"),
        sa.CheckConstraint("verification_mode IN ('s3_checksum','read_back')", name="bulk_upload_mode_ck"),
    )
    op.create_index("bulk_upload_pending_idx", "bulk_upload_admissions", ["vault_id", "updated_at"],
                    postgresql_where=sa.text("status='pending'"))
    op.create_table(
        "bulk_upload_exclusions",
        sa.Column("group_id", sa.String(36), sa.ForeignKey("bulk_upload_admissions.group_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("path", sa.Text(), primary_key=True),
    )
    op.create_index("file_paths_vault_admission_idx", "file_paths", ["vault_id", "id"],
                    postgresql_where=sa.text("valid_to IS NULL"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM bulk_upload_admissions WHERE status='pending' LIMIT 1")).first():
        raise RuntimeError("Cancel or finish pending bulk upload admissions before downgrade")
    op.drop_index("file_paths_vault_admission_idx", table_name="file_paths")
    op.drop_table("bulk_upload_exclusions")
    op.drop_table("bulk_upload_admissions")
