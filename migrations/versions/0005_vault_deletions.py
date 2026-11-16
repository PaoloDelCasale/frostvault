"""Vault Deletion of a Vault Tombstone (#717, ADR-0047).

``vault_deletions`` holds the Owner's consent to delete one Tombstone: at most
one per Vault, never before seven days after the request, and only while the
Vault is a Tombstone. ``status`` is ``pending`` until the worker claims it,
``running`` while it deletes the Vault's prefix (``heartbeat_at`` is refreshed
meanwhile) and ``failed`` after an S3 error or leftovers, until the Owner
retries or cancels. The row goes with its Vault.

Removing the catalog of a deleted Vault crosses the guards that keep evidence
(an Archive Version leaves only with its cleanup proof; an attempt or Upload
Snapshot with open uncertainty is never collected). After a fresh listing has
proved the Vault's whole prefix empty, nothing in the cloud is left for that
evidence to describe: ``proven_empty_at`` of the running deletion lets those
DELETE guards pass for that Vault's rows, and only in that state. Every other
guard, and every INSERT/UPDATE check, is unchanged.
"""
from __future__ import annotations

from alembic import op


revision = "0005_vault_deletions"
down_revision = "0004_local_part_digests"
branch_labels = None
depends_on = None


SCHEMA = """
CREATE TABLE vault_deletions (
    vault_id bigint PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'failed')),
    actor text NOT NULL CHECK (actor IN ('owner', 'emergency')),
    reason text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 500),
    requested_at timestamptz NOT NULL,
    -- The same fixed delay as Delayed Cloud Deletion.
    run_after timestamptz NOT NULL CHECK (run_after >= requested_at + interval '7 days'),
    preview jsonb NOT NULL CHECK (jsonb_typeof(preview) = 'object'),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    heartbeat_at timestamptz,
    failed_at timestamptz,
    failure jsonb CHECK (failure IS NULL OR jsonb_typeof(failure) = 'object'),
    proven_empty_at timestamptz,
    CHECK ((status = 'running') = (heartbeat_at IS NOT NULL)),
    CHECK ((status = 'failed') = (failed_at IS NOT NULL AND failure IS NOT NULL)),
    CHECK (proven_empty_at IS NULL OR status = 'running')
);

-- Only a Tombstone is deleted (ADR-0047).
CREATE FUNCTION vault_deletions_tombstone_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM vaults WHERE id = NEW.vault_id AND lifecycle = 'tombstone'
    ) THEN
        RAISE EXCEPTION 'only a Vault Tombstone can be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER vault_deletions_tombstone_only
    BEFORE INSERT OR UPDATE ON vault_deletions
    FOR EACH ROW EXECUTE FUNCTION vault_deletions_tombstone_only();

-- The running Vault Deletion of this Vault proved its prefix empty.
CREATE FUNCTION vault_deletion_proven(p_vault_id bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM vault_deletions d
        WHERE d.vault_id = p_vault_id AND d.status = 'running'
          AND d.proven_empty_at IS NOT NULL
    )
$$;

CREATE FUNCTION vault_deletion_proven_for_file(p_file_id uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM files f JOIN vault_deletions d ON d.vault_id = f.vault_id
        WHERE f.id = p_file_id AND d.status = 'running'
          AND d.proven_empty_at IS NOT NULL
    )
$$;

DROP TRIGGER archive_versions_removed_with_proof ON archive_versions;
CREATE TRIGGER archive_versions_removed_with_proof
    BEFORE DELETE ON archive_versions
    FOR EACH ROW WHEN (NOT vault_deletion_proven_for_file(OLD.file_id))
    EXECUTE FUNCTION archive_versions_removed_with_proof();

DROP TRIGGER upload_snapshots_guard ON upload_snapshots;
CREATE TRIGGER upload_snapshots_guard
    BEFORE UPDATE ON upload_snapshots
    FOR EACH ROW EXECUTE FUNCTION upload_snapshots_guard();
CREATE TRIGGER upload_snapshots_guard_delete
    BEFORE DELETE ON upload_snapshots
    FOR EACH ROW WHEN (NOT vault_deletion_proven(OLD.vault_id))
    EXECUTE FUNCTION upload_snapshots_guard();

DROP TRIGGER upload_attempts_guard ON upload_attempts;
CREATE TRIGGER upload_attempts_guard
    BEFORE INSERT OR UPDATE ON upload_attempts
    FOR EACH ROW EXECUTE FUNCTION upload_attempts_guard();
CREATE TRIGGER upload_attempts_guard_delete
    BEFORE DELETE ON upload_attempts
    FOR EACH ROW WHEN (NOT vault_deletion_proven_for_file(OLD.file_id))
    EXECUTE FUNCTION upload_attempts_guard();

DROP TRIGGER copy_attempts_guard ON copy_attempts;
CREATE TRIGGER copy_attempts_guard
    BEFORE INSERT OR UPDATE ON copy_attempts
    FOR EACH ROW EXECUTE FUNCTION copy_attempts_guard();
CREATE TRIGGER copy_attempts_guard_delete
    BEFORE DELETE ON copy_attempts
    FOR EACH ROW WHEN (NOT vault_deletion_proven(OLD.vault_id))
    EXECUTE FUNCTION copy_attempts_guard();

DROP TRIGGER cleanup_candidates_guard ON cleanup_candidates;
CREATE TRIGGER cleanup_candidates_guard
    BEFORE INSERT OR UPDATE ON cleanup_candidates
    FOR EACH ROW EXECUTE FUNCTION cleanup_candidates_guard();
CREATE TRIGGER cleanup_candidates_guard_delete
    BEFORE DELETE ON cleanup_candidates
    FOR EACH ROW WHEN (NOT vault_deletion_proven_for_file(OLD.file_id))
    EXECUTE FUNCTION cleanup_candidates_guard();
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0005_vault_deletions has no downgrade: restore a metadata backup instead"
    )
