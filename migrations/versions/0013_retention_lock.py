"""Retention Lock: deletions deferred to the RetainUntilDate (#736, ADR-0050).

A Vault created with a Retention Lock has a Vault Bucket with S3 Object Lock
in Governance mode and a default retention of ``vault_targets.retention_lock_days``
(0010). FrostVault never holds ``s3:BypassGovernanceRetention``, so a locked
Archive Version cannot be deleted before its ``RetainUntilDate``: every
deletion that meets one is deferred, never failed.

``retained_versions`` holds the superseded Archive Versions of a locked Vault
whose exact deletion is deferred: what a Current Snapshot cleanup (an upload,
a rename or a storage-class copy) superseded while the version was still
locked. The cleanup hands the exact version (key and VersionId) over to this
table and records its candidate as handled (``cleanup_candidates.deleted_at``),
so the Archive Version row leaves the catalog with its proof and the file is
not held by a version that cannot be deleted for months. From then on this row
is the only deletion authority for that version: a background sweep deletes it
by exact VersionId once ``retain_until`` has passed, and removes the row when
S3 answered (or the version is proven absent). Listings that check a key for
unknown versions accept these exact versions. Only a Vault whose target has a
Retention Lock can hand a version over; the identity of a row never changes,
only its ``retain_until`` (a fresher ``GetObjectRetention``) and the attempts.

``vault_deletions.retained_until``/``retained_count``: a Vault Deletion that
deleted everything it could and found only locked versions left waits as
``pending`` for the next run; ``retained_count`` locked versions remain until
``retained_until`` (the latest RetainUntilDate). NULL when nothing is locked.

The Delayed Cloud Deletion of a locked Current needs no column: its Job waits
``pending_delay`` with ``run_after`` at the RetainUntilDate. Nothing here calls
S3 and nothing is backfilled.
"""
from __future__ import annotations

from alembic import op


revision = "0013_retention_lock"
down_revision = "0012_billed_cost"
branch_labels = None
depends_on = None


SCHEMA = """
CREATE TABLE retained_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vault_id bigint NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    object_key text COLLATE "C" NOT NULL,
    provider_version_id text COLLATE "C" NOT NULL,
    size bigint NOT NULL,
    storage_class text COLLATE "C",
    retain_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    last_attempt_at timestamptz,
    last_error text COLLATE "C",
    CONSTRAINT retained_versions_key_ck CHECK (object_key <> ''),
    CONSTRAINT retained_versions_version_ck CHECK (provider_version_id NOT IN ('', 'null')),
    CONSTRAINT retained_versions_size_ck CHECK (size >= 0),
    CONSTRAINT retained_versions_attempts_ck CHECK (attempts >= 0),
    CONSTRAINT retained_versions_version_uq UNIQUE (vault_id, object_key, provider_version_id)
);

CREATE INDEX retained_versions_due_idx ON retained_versions (retain_until, vault_id);

-- Only a Vault with a Retention Lock defers a deletion; a row names one exact
-- version for life.
CREATE FUNCTION retained_versions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1 FROM vaults v JOIN vault_targets t ON t.id = v.target_id
            WHERE v.id = NEW.vault_id AND t.retention_lock_days IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'only a Vault with a Retention Lock defers a deletion'
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.created_at := now();
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.vault_id, NEW.object_key, NEW.provider_version_id, NEW.size,
        NEW.storage_class, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.vault_id, OLD.object_key, OLD.provider_version_id, OLD.size,
        OLD.storage_class, OLD.created_at) THEN
        RAISE EXCEPTION 'a retained version names one exact version'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER retained_versions_guard
    BEFORE INSERT OR UPDATE ON retained_versions
    FOR EACH ROW EXECUTE FUNCTION retained_versions_guard();

ALTER TABLE vault_deletions
    ADD COLUMN retained_until timestamptz,
    ADD COLUMN retained_count integer,
    ADD CONSTRAINT vault_deletions_retained_ck CHECK (
        (retained_until IS NULL) = (retained_count IS NULL)
        AND (retained_count IS NULL OR retained_count > 0)
    );
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0013_retention_lock has no downgrade: restore a metadata backup instead"
    )
