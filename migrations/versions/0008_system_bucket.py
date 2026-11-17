"""The System Bucket FrostVault creates for metadata backups (#734, ADR-0049).

``system_bucket`` holds at most one row: the generated
``frostvault-system-<8 random [a-z0-9]>`` name and its region, saved before
the bucket is created so an interrupted creation resumes on the same name.
``ready_at`` is NULL until the bucket is created, configured and verified.
An operator-supplied ``METADATA_BACKUP_BUCKET`` is configuration, not a row.

``metadata_backup_runs.s3_bucket`` names the bucket an off-host copy went to.
It is NULL for every earlier run: those went to the shared bucket, which is
not recorded, and nothing is backfilled.
"""
from __future__ import annotations

from alembic import op


revision = "0008_system_bucket"
down_revision = "0007_duplicate_cleanup_totals"
branch_labels = None
depends_on = None


SCHEMA = """
CREATE TABLE system_bucket (
    id smallint PRIMARY KEY DEFAULT 1,
    name text COLLATE "C" NOT NULL,
    region text COLLATE "C" NOT NULL,
    reserved_at timestamptz NOT NULL,
    ready_at timestamptz,
    CONSTRAINT system_bucket_single_row_ck CHECK (id = 1),
    CONSTRAINT system_bucket_name_ck CHECK (name ~ '^frostvault-system-[a-z0-9]{8}$'),
    CONSTRAINT system_bucket_region_ck CHECK (region ~ '^[a-z0-9-]+$'),
    CONSTRAINT system_bucket_ready_ck CHECK (ready_at IS NULL OR ready_at >= reserved_at)
);

ALTER TABLE metadata_backup_runs
    ADD COLUMN s3_bucket text COLLATE "C",
    ADD CONSTRAINT metadata_backup_runs_s3_bucket_ck
        CHECK (s3_bucket IS NULL OR (s3_key IS NOT NULL AND btrim(s3_bucket) <> ''));
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0008_system_bucket has no downgrade: restore a metadata backup instead"
    )
