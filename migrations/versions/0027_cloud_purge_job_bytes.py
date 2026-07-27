"""Correct per-file byte totals for permanent cloud purge jobs.

Revision ID: 0027_cloud_purge_job_bytes
Revises: 0026_invite_revocation
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0027_cloud_purge_job_bytes"
down_revision: str | None = "0026_invite_revocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE jobs
            SET total_bytes = COALESCE(
                (
                    SELECT SUM(COALESCE(item.size_bytes, 0))
                    FROM cloud_deletion_items AS item
                    WHERE item.job_id = jobs.id
                ),
                0
            )
            WHERE action = 'cloud-purge'
            """
        )
    )


def downgrade() -> None:
    pass
