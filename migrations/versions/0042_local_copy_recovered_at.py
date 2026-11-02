"""Record when a Local Copy was written back by a recovery.

Automatic local cleanup must give a recovered file a full retention window
from the recovery itself (#409, #363 D6). The recovered file's mtime cannot be
trusted for this: rclone restores the original modification time, so a photo
from 2019 would look old enough to be freed again on the next pass.

Revision ID: 0042_local_copy_recovered_at
Revises: 0041_cloud_history_policy
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0042_local_copy_recovered_at"
down_revision: str | None = "0041_cloud_history_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("local_copies") as batch:
        batch.add_column(sa.Column("recovered_at", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("local_copies") as batch:
        batch.drop_column("recovered_at")
