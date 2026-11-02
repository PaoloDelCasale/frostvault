"""Hold back new content that reappears at the path of a cloud-only file.

When a Local Copy was absent (freed or deleted via SMB) its verified Archive
Version is the only copy. Different content later observed at the same path
must not be uploaded: a verified Current Snapshot upload deletes the previous
version (#411, #363 D7). ``local_copies.cloud_only_version_id`` remembers that
only copy until the reappeared content proves identical or the owner confirms
the replacement; a ``cloud_only_conflict`` catalog incident is the open
problem, and also a notification event.

Revision ID: 0043_cloud_only_conflict
Revises: 0042_local_copy_recovered_at
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0043_cloud_only_conflict"
down_revision: str | None = "0042_local_copy_recovered_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("local_copies") as batch:
        batch.add_column(
            sa.Column("cloud_only_version_id", sa.String(36), nullable=True)
        )
    with op.batch_alter_table("catalog_incidents") as batch:
        batch.drop_constraint("catalog_incidents_kind_ck", type_="check")
        batch.create_check_constraint(
            "catalog_incidents_kind_ck",
            "kind IN ('archive_version_missing', 'cloud_only_conflict')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM catalog_incidents WHERE kind='cloud_only_conflict'")
    with op.batch_alter_table("catalog_incidents") as batch:
        batch.drop_constraint("catalog_incidents_kind_ck", type_="check")
        batch.create_check_constraint(
            "catalog_incidents_kind_ck",
            "kind IN ('archive_version_missing')",
        )
    with op.batch_alter_table("local_copies") as batch:
        batch.drop_column("cloud_only_version_id")
