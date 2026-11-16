"""What Automatic Duplicate Cleanup deleted so far, per Vault (#729, ADR-0048).

The totals outlive the history retention that collects completed Jobs: each
Vault keeps how many members of its automatic deletions were deleted, their
bytes and when the last one was. They grow in the transaction that records a
member as deleted; a revoked, failed or cancelled member never counts, and a
manual deletion never does.

All three are NULL until a first automatic deletion is recorded: an upgrade
invents no past totals, even when older completed Jobs are still listed.
"""
from __future__ import annotations

from alembic import op


revision = "0007_duplicate_cleanup_totals"
down_revision = "0006_duplicate_cleanup"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vaults
    ADD COLUMN duplicate_cleanup_deleted_count bigint,
    ADD COLUMN duplicate_cleanup_deleted_bytes bigint,
    ADD COLUMN duplicate_cleanup_last_deleted_at timestamptz,
    ADD CONSTRAINT vaults_duplicate_cleanup_totals_ck
        CHECK ((duplicate_cleanup_deleted_count IS NULL) = (duplicate_cleanup_deleted_bytes IS NULL)
               AND (duplicate_cleanup_deleted_count IS NULL) = (duplicate_cleanup_last_deleted_at IS NULL)
               AND COALESCE(duplicate_cleanup_deleted_count, 0) >= 0
               AND COALESCE(duplicate_cleanup_deleted_bytes, 0) >= 0);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0007_duplicate_cleanup_totals has no downgrade: restore a metadata backup instead"
    )
