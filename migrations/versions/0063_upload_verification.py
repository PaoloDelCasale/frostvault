"""Independent new-upload evidence and per-Vault verification overrides.

Revision ID: 0063_upload_verification
Revises: 0062_cloud_preflight_gate
"""
from alembic import op
import sqlalchemy as sa

revision = "0063_upload_verification"
down_revision = "0062_cloud_preflight_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vault_operation_policies", sa.Column("upload_verification", sa.String(16), nullable=True))
    op.create_check_constraint("vault_upload_verification_mode", "vault_operation_policies",
                               "upload_verification IS NULL OR upload_verification IN ('s3_checksum','read_back')")
    for table in ("jobs", "archive_versions"):
        op.add_column(table, sa.Column("upload_verification_requested", sa.String(16), nullable=True))
        op.add_column(table, sa.Column("upload_snapshot_json", sa.Text(), nullable=True))
        op.create_check_constraint(f"{table}_upload_verification_mode", table,
                                   "upload_verification_requested IS NULL OR upload_verification_requested IN ('s3_checksum','read_back')")
    op.add_column("archive_versions", sa.Column("verification_method", sa.String(16), nullable=True))
    op.add_column("archive_versions", sa.Column("verification_fallback_reason", sa.String(32), nullable=True))
    op.add_column("archive_versions", sa.Column("verification_egress_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint("archive_verification_method", "archive_versions",
                               "verification_method IS NULL OR verification_method IN ('s3_checksum','read_back')")
    # No backfill: legacy trust/digests/jobs remain exactly as recorded.


def downgrade():
    op.drop_constraint("archive_verification_method", "archive_versions", type_="check")
    for field in ("verification_egress_bytes", "verification_fallback_reason", "verification_method"):
        op.drop_column("archive_versions", field)
    for table in ("archive_versions", "jobs"):
        op.drop_constraint(f"{table}_upload_verification_mode", table, type_="check")
        op.drop_column(table, "upload_snapshot_json")
        op.drop_column(table, "upload_verification_requested")
    op.drop_constraint("vault_upload_verification_mode", "vault_operation_policies", type_="check")
    op.drop_column("vault_operation_policies", "upload_verification")
