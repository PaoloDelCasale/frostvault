"""Rename analysis without a quadratic scan (#759).

The watcher's rename analysis (#621) asks, for each unfingerprinted Local
Copy, whether a fingerprinted Local Copy of the same size went missing within
the rename window. Without an index for that question PostgreSQL walked every
fingerprinted file of the Vault through ``files_rename_digest_idx`` once per
candidate: (files without fingerprint) x (files with fingerprint).

``files_rename_missing_size_idx`` holds only the rename sources, the
fingerprinted regular Local Copies that went missing and are not held, so
the lookup is a probe by (Vault, size, observation time) into a usually empty
index. Its predicate is the one of ``awaiting_rename_analysis_sql``. No data
changes, no S3 call.
"""
from __future__ import annotations

from alembic import op


revision = "0016_rename_missing_size_idx"
down_revision = "0015_retained_version_retry"
branch_labels = None
depends_on = None


SCHEMA = """
CREATE INDEX files_rename_missing_size_idx
    ON files (vault_id, local_size, local_observed_at)
    WHERE local_presence = 'missing' AND local_file_type = 'regular'
      AND local_sha256 IS NOT NULL AND held_version_id IS NULL;
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0016_rename_missing_size_idx has no downgrade: restore a metadata backup instead"
    )
