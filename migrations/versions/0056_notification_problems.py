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
    op.add_column("notifications", sa.Column("problem_status", sa.Text(), nullable=True))
    op.add_column("notifications", sa.Column("resolved_at", sa.Text(), nullable=True))
    op.create_check_constraint("notifications_problem_status_ck", "notifications",
                               "problem_status IN ('open', 'resolved')")
    op.create_index("notifications_open_problems_idx", "notifications", ["user_id"],
                    postgresql_where=sa.text("problem_status='open'"))
    op.create_table(
        "protection_pending",
        sa.Column("vault_file_id", sa.String(36), sa.ForeignKey("vault_files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("pending_since", sa.Text(), nullable=False),
    )
    op.add_column("vault_operation_policies", sa.Column("suspended_at", sa.Text(), nullable=True))
    op.execute("UPDATE vault_operation_policies SET suspended_at=CURRENT_TIMESTAMP::text WHERE NOT auto_upload")
    # Notification history only: never touch Local Copies, Jobs or cloud evidence.
    op.execute("""
        UPDATE notifications n SET event='recovery_completed',
            title_key='notification.recovery_completed.title',
            body_key='notification.recovery_completed.body',
            dedupe_key='job:' || n.job_id::text || chr(58) || 'recovery_completed'
        FROM jobs j WHERE n.job_id=j.id AND n.event='job_completed'
            AND j.action='recover' AND j.origin='manual'
    """)
    op.execute("""
        UPDATE notifications n SET event='recovery_failed',
            title_key='notification.recovery_failed.title',
            body_key='notification.recovery_failed.body',
            dedupe_key='job:' || n.job_id::text || chr(58) || 'recovery_failed'
        FROM jobs j WHERE n.job_id=j.id AND n.event='job_failed'
            AND j.action='recover' AND j.origin='manual'
    """)
    op.execute("DELETE FROM notifications WHERE event LIKE 'local_cleanup.%' OR event='job_completed'")
    op.execute("""
        DELETE FROM notifications n USING jobs j WHERE n.job_id=j.id
            AND n.event='job_failed' AND j.action NOT IN ('upload','recover')
    """)
    op.execute("""
        UPDATE notifications SET problem_status='open'
        WHERE event IN ('archive_version_missing', 'cloud_only_conflict',
                        'protection_blocked', 'file_unreadable', 'internal_error', 'job_failed')
    """)
    op.execute("""
        UPDATE notifications SET dedupe_key='blocked:' || vault_id::text || chr(58) || 'source'
        WHERE event='protection_blocked' AND message_params::jsonb->>'component'='source'
    """)


def downgrade() -> None:
    op.drop_column("vault_operation_policies", "suspended_at")
    op.drop_table("protection_pending")
    op.drop_index("notifications_open_problems_idx", table_name="notifications")
    op.drop_constraint("notifications_problem_status_ck", "notifications", type_="check")
    for column in ("resolved_at", "problem_status"):
        op.drop_column("notifications", column)
