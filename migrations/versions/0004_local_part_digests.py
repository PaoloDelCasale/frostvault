"""Part digests recorded with a Local Copy fingerprint (#703).

The scan verification hashes a Local Copy of at least 8 MiB (the multipart
threshold) and, in the same read, the SHA-256 of each of its multipart parts,
cut with the part size of the Upload Snapshot. Its upload then builds the
snapshot from the catalog instead of reading the file a first time, and still
checks every part it reads while sending against these digests.

``local_part_size`` and ``local_part_sha256`` describe the bytes of
``local_sha256`` and nothing else: both or neither, only next to a digest and
only for files of at least 8 MiB. A digest that changes without new parts
clears them (``files_local_parts_follow_digest``), so no path can leave parts
of other content behind. NULL: not recorded (a file under 8 MiB, or verified
before this revision). Nothing is backfilled.
"""
from __future__ import annotations

from alembic import op


revision = "0004_local_part_digests"
down_revision = "0003_job_admitted_generation"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE files
    ADD COLUMN local_part_size bigint,
    ADD COLUMN local_part_sha256 bytea[],
    -- The parts of the recorded digest: both or neither, next to a digest,
    -- for 8 MiB or more, at most 10,000 parts of 8 MiB to 1 GiB, one per
    -- part of the recorded size.
    ADD CONSTRAINT files_local_parts_ck CHECK (
        (local_part_size IS NULL) = (local_part_sha256 IS NULL)
        AND (local_part_sha256 IS NULL OR (
            local_sha256 IS NOT NULL
            AND local_size >= 8388608
            AND local_part_size BETWEEN 8388608 AND 1073741824
            AND array_ndims(local_part_sha256) = 1
            AND cardinality(local_part_sha256) BETWEEN 1 AND 10000
            AND cardinality(local_part_sha256)
                = (local_size + local_part_size - 1) / local_part_size
        ))
    );

-- A digest written without its parts drops the parts recorded with the
-- previous one: they described other bytes.
CREATE FUNCTION files_local_parts_follow_digest() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.local_part_size := NULL;
    NEW.local_part_sha256 := NULL;
    RETURN NEW;
END
$$;

CREATE TRIGGER files_local_parts_follow_digest
    BEFORE UPDATE OF local_sha256 ON files
    FOR EACH ROW
    WHEN (NEW.local_sha256 IS DISTINCT FROM OLD.local_sha256
          AND OLD.local_part_sha256 IS NOT NULL
          AND NEW.local_part_sha256 IS NOT DISTINCT FROM OLD.local_part_sha256)
    EXECUTE FUNCTION files_local_parts_follow_digest();
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0004_local_part_digests has no downgrade: restore a metadata backup instead"
    )
