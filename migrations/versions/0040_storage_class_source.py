"""Persist why an Archive Version is in its current storage class.

Revision ID: 0040_storage_class_source
Revises: 0039_catalog_incidents
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0040_storage_class_source"
down_revision: str | None = "0039_catalog_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("archive_versions") as batch:
        batch.add_column(
            sa.Column(
                "storage_class_source",
                sa.Text(),
                nullable=False,
                server_default="unknown",
            )
        )
        batch.create_check_constraint(
            "archive_versions_storage_class_source_ck",
            "storage_class_source IN ('upload', 'discovered', 'policy', 'manual', 'unknown')",
        )
    op.execute(
        """
        UPDATE archive_versions
        SET storage_class_source = CASE
            WHEN origin = 'upload' THEN 'upload'
            WHEN origin = 'discovered' THEN 'discovered'
            ELSE 'unknown'
        END
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("archive_versions") as batch:
        batch.drop_constraint(
            "archive_versions_storage_class_source_ck",
            type_="check",
        )
        batch.drop_column("storage_class_source")
