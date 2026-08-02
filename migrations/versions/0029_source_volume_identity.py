"""Persist markerless Source Volume identities.

Revision ID: 0029_source_volume_identity
Revises: 0028_source_areas
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_source_volume_identity"
down_revision: str | None = "0028_source_areas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_volumes",
        sa.Column("alias", sa.Text(), primary_key=True),
        sa.Column("fingerprint_version", sa.Text(), nullable=False),
        sa.Column("expected_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        # Dedupe an unresolved transition across monitor passes/restarts. This
        # stores only an opaque fingerprint or a non-sensitive error category.
        sa.Column("last_alert_token", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("source_volumes")
