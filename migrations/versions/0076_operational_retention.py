"""Bounded operational retention cursors and reference/eligibility indexes.

Temporary ADR-0035 incremental migration, to be folded into the new baseline.
No content, evidence, trust, consent or existing history is changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0076_operational_retention"
down_revision = "0075_file_state"
branch_labels = None
depends_on = None

INDEXES = (
    ("jobs_retention_cancelled_idx", "jobs", ["id"], "status='cancelled' AND action='upload'"),
    ("notifications_retention_closed_idx", "notifications", ["id"],
     "read_at IS NOT NULL AND (problem_status IS NULL OR problem_status='resolved')"),
    ("notification_deliveries_retention_closed_idx", "notification_deliveries", ["id"], "status IN ('delivered','failed')"),
    ("notification_deliveries_notification_idx", "notification_deliveries", ["notification_id"], None),
    ("notifications_job_reference_idx", "notifications", ["job_id"], "job_id IS NOT NULL"),
    ("notifications_vault_open_reference_idx", "notifications", ["vault_id"], "problem_status='open'"),
    ("audit_events_job_reference_idx", "audit_events", ["job_id"], "job_id IS NOT NULL"),
    ("catalog_incidents_open_file_reference_idx", "catalog_incidents", ["vault_file_id"], "status='open'"),
    # The existing original_job_id index excludes settled intents: those remain
    # independent lineage and must also exclude a Job from history deletion.
    ("upload_intents_original_reference_idx", "upload_intents", ["original_job_id"], None),
)


def upgrade():
    op.create_table("operational_retention_cursors",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("last_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("through_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("last_id >= 0 AND through_id >= last_id", name="operational_retention_cursor_bounds"),
        sa.CheckConstraint("name IN ('jobs','notifications','deliveries','audit')", name="operational_retention_cursor_name"))
    op.execute("INSERT INTO operational_retention_cursors(name) VALUES ('jobs'),('notifications'),('deliveries'),('audit')")
    for name, table, columns, predicate in INDEXES:
        op.create_index(name, table, columns,
            postgresql_where=sa.text(predicate) if predicate else None)


def downgrade():
    for name, table, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_table("operational_retention_cursors")
