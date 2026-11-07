"""Journal of the multipart UploadIds an upload intent created (#491)."""
from alembic import op
import sqlalchemy as sa

revision = "0066_upload_multipart_journal"
down_revision = "0065_upload_remediation"
branch_labels = None
depends_on = None


def upgrade():
    # An UploadId is recorded BEFORE its first part is sent, so an interrupted upload
    # can later abort exactly the multipart upload its own intent created, and no
    # other. Metadata-only: no row is synthesized for existing intents.
    op.create_table(
        "upload_multiparts",
        sa.Column("intent_id", sa.Text(), sa.ForeignKey("upload_intents.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("upload_id", sa.Text(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("closed_at", sa.Text()),
        sa.CheckConstraint("state IN ('open','closed')", name="upload_multipart_state"),
        sa.CheckConstraint("(state = 'closed') = (closed_at IS NOT NULL)", name="upload_multipart_closed_at"),
    )
    op.create_index("upload_multiparts_open_idx", "upload_multiparts", ["intent_id"],
                    postgresql_where=sa.text("state = 'open'"))


def downgrade():
    # An open row of an intent that is still owed reconciliation (send started, not
    # settled) is the only proof that lets the application abort its incomplete
    # multipart upload exactly. Rows of settled or never-sent intents carry no
    # obligation, and neither do those of a cancelled intent (its original or a bound
    # Job was cancelled): it can never be resumed, so nothing could use the row.
    pending = op.get_bind().execute(sa.text(
        "SELECT 1 FROM upload_multiparts m JOIN upload_intents i ON i.id = m.intent_id "
        "WHERE m.state = 'open' AND i.send_started AND NOT i.settled "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = i.original_job_id AND j.status = 'cancelled') "
        "AND NOT EXISTS (SELECT 1 FROM upload_intent_jobs b JOIN jobs j ON j.id = b.job_id "
        "WHERE b.intent_id = i.id AND j.status = 'cancelled') LIMIT 1")).first()
    if pending:
        raise RuntimeError("Abort or reconcile open multipart uploads of interrupted uploads before downgrade")
    op.drop_index("upload_multiparts_open_idx", table_name="upload_multiparts")
    op.drop_table("upload_multiparts")
