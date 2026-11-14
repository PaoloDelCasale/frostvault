"""Original owner and permission bits of each Upload Snapshot (#679).

The first linear migration after ``0001_baseline`` froze. Upload Snapshots
record the uid, gid and permission bits (``stat.S_IMODE``) of the hashed Local
Copy next to its ``mtime_ns``, so a Recovery can give the file back its
original owner and mode. Snapshots made before this revision have none of the
three: nothing is backfilled or invented.

The immutability guard is replaced so the new columns are immutable too.
"""
from __future__ import annotations

from alembic import op


revision = "0002_file_ownership"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE upload_snapshots
    ADD COLUMN owner_uid bigint,
    ADD COLUMN owner_gid bigint,
    ADD COLUMN file_mode integer,
    -- All three or none; ids fit uid_t/gid_t without (uid_t)-1, the mode is
    -- the permission bits only (no file type).
    ADD CONSTRAINT upload_snapshots_ownership_ck CHECK (
        (owner_uid IS NULL AND owner_gid IS NULL AND file_mode IS NULL)
        OR (owner_uid BETWEEN 0 AND 4294967294
            AND owner_gid BETWEEN 0 AND 4294967294
            AND file_mode BETWEEN 0 AND 4095)
    );

CREATE OR REPLACE FUNCTION upload_snapshots_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.closed_at IS NULL THEN
            RAISE EXCEPTION 'an open Upload Snapshot is never collected'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF (NEW.id, NEW.vault_id, NEW.file_id, NEW.path, NEW.path_since, NEW.source_root,
        NEW.root_identity, NEW.provider, NEW.local_generation, NEW.size, NEW.mtime_ns,
        NEW.owner_uid, NEW.owner_gid, NEW.file_mode,
        NEW.plaintext_sha256, NEW.part_size, NEW.part_sha256, NEW.verification,
        NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.vault_id, OLD.file_id, OLD.path, OLD.path_since, OLD.source_root,
        OLD.root_identity, OLD.provider, OLD.local_generation, OLD.size, OLD.mtime_ns,
        OLD.owner_uid, OLD.owner_gid, OLD.file_mode,
        OLD.plaintext_sha256, OLD.part_size, OLD.part_sha256, OLD.verification,
        OLD.created_at)
       OR (OLD.closed_at IS NOT NULL
           AND NEW.closed_at IS DISTINCT FROM OLD.closed_at) THEN
        RAISE EXCEPTION 'an Upload Snapshot is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.closed_at IS NOT NULL AND OLD.closed_at IS NULL AND EXISTS (
        SELECT 1 FROM upload_attempts
        WHERE snapshot_id = NEW.id AND settled_at IS NULL
    ) THEN
        RAISE EXCEPTION 'an Upload Snapshot with an unsettled attempt stays open'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0002_file_ownership has no downgrade: restore a metadata backup instead"
    )
