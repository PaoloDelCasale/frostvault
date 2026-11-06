"""Durable installation S3 readiness cache. Metadata only; no provider calls."""
from alembic import op
import sqlalchemy as sa

revision = "0062_cloud_preflight_gate"
down_revision = "0061_aggregate_dirty_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloud_preflight_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("remediation", sa.Text()),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("next_probe_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id=1", name="cloud_preflight_singleton"),
        sa.CheckConstraint("status IN ('ready','blocked')", name="cloud_preflight_status"),
    )


def downgrade() -> None:
    op.drop_table("cloud_preflight_state")
