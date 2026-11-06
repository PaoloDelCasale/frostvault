"""Target-scoped upload remediation and metadata-only discovery inbox."""
from alembic import op
import sqlalchemy as sa

revision = "0065_upload_remediation"
down_revision = "0064_upload_provenance"
branch_labels = None
depends_on = None


def upgrade():
    # Composite references prevent cross-file/Vault history or authority links.
    for table in ("jobs", "archive_versions", "upload_intents"):
        op.create_index(f"{table}_remediation_scope_uq", table,
                        ["id", "vault_file_id", "vault_id"], unique=True)
    op.create_table("upload_remediations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("target_key", sa.Text(), nullable=False, unique=True),
        sa.Column("vault_id", sa.BigInteger(), sa.ForeignKey("vaults.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("vault_file_id", sa.Text(), sa.ForeignKey("vault_files.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("intent_id", sa.Text()),
        sa.Column("archive_version_id", sa.Text()),
        sa.Column("target_json", sa.Text(), nullable=False),
        sa.Column("expected_sha256", sa.Text()),
        sa.Column("snapshot_json", sa.Text()),
        sa.Column("requested_mode", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("phase", sa.Text(), nullable=False, server_default="verification"),
        sa.Column("reason", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due_at", sa.Text(), nullable=False),
        sa.Column("current_job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), unique=True),
        sa.Column("resolved_by_job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.Text()),
        sa.Column("cleanup_inventory_job_id", sa.BigInteger(), sa.ForeignKey("snapshot_cleanup_inventories.job_id", ondelete="RESTRICT")),
        sa.UniqueConstraint("id", "vault_file_id", "vault_id", name="upload_remediations_scope_uq"),
        sa.UniqueConstraint("archive_version_id", name="upload_remediations_version_uq"),
        sa.UniqueConstraint("intent_id", name="upload_remediations_intent_uq"),
        sa.ForeignKeyConstraint(["current_job_id", "vault_file_id", "vault_id"],
            ["jobs.id", "jobs.vault_file_id", "jobs.vault_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by_job_id", "vault_file_id", "vault_id"],
            ["jobs.id", "jobs.vault_file_id", "jobs.vault_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["archive_version_id", "vault_file_id", "vault_id"],
            ["archive_versions.id", "archive_versions.vault_file_id", "archive_versions.vault_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["intent_id", "vault_file_id", "vault_id"],
            ["upload_intents.id", "upload_intents.vault_file_id", "upload_intents.vault_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("intent_id IS NOT NULL OR archive_version_id IS NOT NULL", name="upload_remediation_exact_target"),
        sa.CheckConstraint("state IN ('pending','queued','retrying','blocked','suspended','cancelled','resolved')", name="upload_remediation_state"),
        sa.CheckConstraint("requested_mode IN ('read_back','s3_checksum')", name="upload_remediation_mode"),
        sa.CheckConstraint("retry_count >= 0", name="upload_remediation_budget"),
    )
    for table, column in (("upload_remediation_originals", "original_job_id"),
                          ("upload_remediation_jobs", "job_id")):
        op.create_table(table,
            sa.Column(column, sa.BigInteger(), primary_key=True),
            sa.Column("remediation_id", sa.Text(), nullable=False),
            sa.Column("vault_file_id", sa.Text(), nullable=False),
            sa.Column("vault_id", sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint([column, "vault_file_id", "vault_id"],
                ["jobs.id", "jobs.vault_file_id", "jobs.vault_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["remediation_id", "vault_file_id", "vault_id"],
                ["upload_remediations.id", "upload_remediations.vault_file_id", "upload_remediations.vault_id"], ondelete="RESTRICT"))
        op.create_index(f"{table}_target_idx", table, ["remediation_id", column])
    op.create_index("upload_remediations_due_idx", "upload_remediations", ["vault_id", "due_at", "id"],
        postgresql_where=sa.text("state IN ('pending','suspended')"))
    op.create_index("upload_remediations_file_idx", "upload_remediations", ["vault_file_id", "created_at"])
    op.create_index("upload_remediations_open_idx", "upload_remediations", ["state", "current_job_id"],
        postgresql_where=sa.text("state <> 'resolved'"))
    op.create_table("upload_remediation_discovery",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("vault_id", sa.BigInteger(), sa.ForeignKey("vaults.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("discovered_at", sa.Text(), nullable=False))
    op.create_index("upload_remediation_discovery_vault_idx", "upload_remediation_discovery", ["vault_id", "job_id"])
    op.create_table("upload_remediation_sweep",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("last_vault_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_tick_at", sa.Text()))
    op.execute("INSERT INTO upload_remediation_sweep(name) VALUES ('discovery'),('admission')")
    # One historical metadata pass, NOT a trust/protection/evidence backfill.
    # Runtime terminal transitions append to this indexed inbox; ticks never scan history.
    op.execute("""INSERT INTO upload_remediation_discovery(job_id,vault_id,discovered_at)
        SELECT j.id,j.vault_id,j.updated_at FROM jobs j
        WHERE j.action='upload' AND j.status='failed' AND NOT j.upload_reconcile_only
          AND (j.archive_version_id IS NOT NULL OR EXISTS (
              SELECT 1 FROM upload_intents i WHERE i.original_job_id=j.id AND i.send_started))""")


def downgrade():
    pending = op.get_bind().execute(sa.text("""SELECT 1 FROM upload_remediations
        WHERE state <> 'resolved' LIMIT 1""")).first()
    inbox = op.get_bind().execute(sa.text("SELECT 1 FROM upload_remediation_discovery LIMIT 1")).first()
    if pending or inbox:
        raise RuntimeError("Resolve retained upload remediation obligations before downgrade")
    for table in ("upload_remediation_sweep", "upload_remediation_discovery", "upload_remediation_jobs",
                  "upload_remediation_originals", "upload_remediations"):
        op.drop_table(table)
    for table in ("upload_intents", "archive_versions", "jobs"):
        op.drop_index(f"{table}_remediation_scope_uq", table_name=table)
