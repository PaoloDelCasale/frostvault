"""Add durable worker claim leases to Jobs (issue #193).

Revision ID: 0033_job_claim_leases
Revises: 0032_notification_inbox
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0033_job_claim_leases"
down_revision: str | None = "0032_notification_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Claims are intentionally nullable so every pre-lease Job remains runnable
    # after upgrade.  A worker writes all three values together; a NULL token or
    # expiry is treated as an unclaimed/stale row by the claim predicate.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("claim_token", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("claimed_at", sa.Text(), nullable=True))
        batch.add_column(sa.Column("claim_expires_at", sa.Text(), nullable=True))

    # The scheduler orders every acquisition by requested_at/id.  Keeping the
    # state and expiry prefix makes expired-lease recovery and ordinary queued
    # admission efficient on both supported backends.
    op.create_index(
        "jobs_claimable_lease_idx",
        "jobs",
        ["status", "claim_expires_at", "requested_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("jobs_claimable_lease_idx", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("claim_expires_at")
        batch.drop_column("claimed_at")
        batch.drop_column("claim_token")
