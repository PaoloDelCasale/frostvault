"""Fixed seven-day deletion consent and stop protection (#428).

No cloud/filesystem calls. Reconcile interrupted destructive work and finish
legacy decommissions with the old release before upgrading.
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_delayed_cloud_deletion"
down_revision = "0058_reconnect_recovery_kit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("""SELECT 1 FROM vault_decommissions WHERE state<>'completed'
        UNION ALL SELECT 1 FROM jobs WHERE action='cloud-purge'
          AND status NOT IN ('pending_delay','completed','failed','cancelled') LIMIT 1""")).first():
        raise RuntimeError("Reconcile interrupted cloud deletions and finish legacy decommissions before upgrading")
    # Old delay/phrase/enablement are not consent for the new workflow.
    bind.execute(sa.text("""UPDATE cloud_deletion_items SET status='skipped'
        WHERE status='pending' AND job_id IN (
            SELECT id FROM jobs WHERE action='cloud-purge' AND status='pending_delay')"""))
    bind.execute(sa.text("""UPDATE jobs SET status='cancelled',claim_token=NULL,claimed_at=NULL,
        claim_expires_at=NULL,message='Legacy deletion cancelled: request a new seven-day delay'
        WHERE action='cloud-purge' AND status='pending_delay'"""))
    op.drop_table("vault_decommissions")
    op.drop_column("vaults", "cloud_deletion_enabled")
    # Preserve historical origin values on terminal Jobs, never run them again.
    op.drop_constraint("jobs_origin_ck", "jobs", type_="check")
    op.create_check_constraint("jobs_origin_ck", "jobs", "origin IN ('manual','automatic','decommission','stop-protection')")
    op.create_table("protection_stops",
                    sa.Column("vault_id", sa.BigInteger(), sa.ForeignKey("vaults.id", ondelete="RESTRICT"), primary_key=True),
                    sa.Column("requested_at", sa.Text(), nullable=False),
                    sa.Column("requested_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
                    sa.Column("cloud_disposition", sa.Text(), nullable=False),
                    sa.Column("group_id", sa.String(36)),
                    sa.Column("reason", sa.Text(), nullable=False),
                    sa.CheckConstraint("cloud_disposition IN ('keep','delete')", name="protection_stops_cloud_ck"))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM protection_stops LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade after stopping folder protection")
    op.drop_table("protection_stops")
    op.execute("UPDATE jobs SET origin='manual' WHERE origin='stop-protection'")
    op.drop_constraint("jobs_origin_ck", "jobs", type_="check")
    op.create_check_constraint("jobs_origin_ck", "jobs", "origin IN ('manual','automatic','decommission')")
    op.add_column("vaults", sa.Column("cloud_deletion_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Legacy operations are not resurrected; only their empty schema is restored.
    op.create_table("vault_decommissions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("vault_id", sa.BigInteger(), sa.ForeignKey("vaults.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("requested_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *[sa.Column(name, sa.Text(), nullable=name in {"completed_at", "error_code", "error_message"})
          for name in ("requested_at", "updated_at", "completed_at", "state", "local_disposition",
                       "cloud_disposition", "local_status", "cloud_status", "reason", "preview_json",
                       "error_code", "error_message")],
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("local_job_group_id", sa.String(36)), sa.Column("cloud_job_group_id", sa.String(36)),
        sa.CheckConstraint("state IN ('quiescing','local_cleanup','cloud_purge','finalizing','blocked','completed')", name="vault_decommissions_state_ck"),
        sa.CheckConstraint("local_disposition IN ('retain','remove')", name="vault_decommissions_local_disposition_ck"),
        sa.CheckConstraint("cloud_disposition IN ('retain','purge')", name="vault_decommissions_cloud_disposition_ck"),
        sa.CheckConstraint("local_status IN ('pending','retained','removing','removed')", name="vault_decommissions_local_status_ck"),
        sa.CheckConstraint("cloud_status IN ('pending','retained','purging','purged')", name="vault_decommissions_cloud_status_ck"),
        sa.CheckConstraint("length(trim(reason)) BETWEEN 3 AND 500", name="vault_decommissions_reason_ck"))
    op.create_index("vault_decommissions_state_idx", "vault_decommissions", ["state", "updated_at"])
