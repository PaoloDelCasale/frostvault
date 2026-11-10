"""One open upload intent per Job, not one intent per Job for ever (#538).

A Job whose Local Copy changed before its send started closes that intent (settled,
snapshot kept intact) and admits a new one with a new snapshot and token. The Job
is the ``original_job_id`` of both, so the former unique constraint becomes a unique
index on the open (unsettled) intents only.

Revision ID: 0069_upload_intent_per_attempt
Revises: 0068_unsupported_state
"""
from alembic import op
import sqlalchemy as sa

revision = "0069_upload_intent_per_attempt"
down_revision = "0068_unsupported_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Metadata-only: no intent, receipt, Job or snapshot is rewritten.
    op.drop_constraint("upload_intents_original_job_id_key", "upload_intents", type_="unique")
    op.create_index("upload_intents_open_original_job_uq", "upload_intents", ["original_job_id"],
                    unique=True, postgresql_where=sa.text("NOT settled"))


def downgrade() -> None:
    # A Job that already admitted a second intent cannot be expressed by the old
    # constraint. Its closed intents are history (receipts, remediation links), so
    # they are never deleted to make the downgrade fit.
    repeated = op.get_bind().execute(sa.text(
        "SELECT 1 FROM upload_intents GROUP BY original_job_id HAVING count(*) > 1 LIMIT 1")).first()
    if repeated:
        raise RuntimeError("A Job admitted more than one upload intent after a source change; downgrade is not possible")
    op.drop_index("upload_intents_open_original_job_uq", table_name="upload_intents")
    op.create_unique_constraint("upload_intents_original_job_id_key", "upload_intents", ["original_job_id"])
