"""Persist the digest associated with an upload Job.

Revision ID: 0035_upload_verification_digest
Revises: 0034_job_claim_leases
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0035_upload_verification_digest"
down_revision: str | None = "0034_job_claim_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The digest belongs to the upload operation's immutable snapshot, not to
    # the mutable Local Copy.  It remains available when a retry observes a
    # different file or a scan clears the Local Copy fingerprint.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column("upload_plaintext_sha256", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("upload_plaintext_sha256")
