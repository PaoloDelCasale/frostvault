"""Fixed notification rules and durable problem/delay episodes (#425).

Revision ID: 0056_notification_problems
Revises: 0055_protection_states
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_notification_problems"
down_revision = "0055_protection_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("problem_key", sa.Text(), nullable=True))
    op.add_column("notifications", sa.Column("problem_status", sa.Text(), nullable=True))
    op.add_column("notifications", sa.Column("resolved_at", sa.Text(), nullable=True))
    op.create_check_constraint("notifications_problem_status_ck", "notifications",
                               "problem_status IN ('open', 'resolved')")
    op.create_table(
        "protection_pending",
        sa.Column("vault_file_id", sa.String(36), sa.ForeignKey("vault_files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("pending_since", sa.Text(), nullable=False),
    )
    op.add_column("vault_operation_policies", sa.Column("suspended_at", sa.Text(), nullable=True))
    op.execute("UPDATE vault_operation_policies SET suspended_at=CURRENT_TIMESTAMP::text WHERE NOT auto_upload")
    # Notification history only: never touch Local Copies, Jobs or cloud evidence.
    op.execute("DELETE FROM notifications WHERE event LIKE 'local_cleanup.%' OR event='job_completed'")
    op.execute("""
        UPDATE notifications SET problem_key=dedupe_key, problem_status='open'
        WHERE event IN ('archive_version_missing', 'cloud_only_conflict',
                        'protection_blocked', 'file_unreadable', 'internal_error', 'job_failed')
    """)


def downgrade() -> None:
    op.drop_column("vault_operation_policies", "suspended_at")
    op.drop_table("protection_pending")
    op.drop_constraint("notifications_problem_status_ck", "notifications", type_="check")
    for column in ("resolved_at", "problem_status", "problem_key"):
        op.drop_column("notifications", column)
