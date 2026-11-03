"""Remove reversible cloud hiding without touching cloud content (#417).

Historical jobs and Delete Markers are retained. Pending hide jobs are cancelled
rather than resumed; no provider call or content disposition is inferred.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_essential_file_actions"
down_revision = "0048_essential_administration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE jobs
        SET status='cancelled', message='Cloud hiding was removed; cloud content retained',
            message_key=NULL, message_params=NULL,
            claim_token=NULL, claimed_at=NULL, claim_expires_at=NULL
        WHERE action='cloud-archive'
          AND status NOT IN ('completed', 'failed', 'cancelled')
    """)
    op.drop_column("directory_aggregates", "action_cloud_archive")


def downgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.add_column(
        "directory_aggregates",
        sa.Column("action_cloud_archive", identifier, nullable=False, server_default="0"),
    )
    # Cancelled jobs are deliberately not revived on downgrade.
    op.execute("UPDATE directory_aggregate_status SET status='rebuild_required'")
