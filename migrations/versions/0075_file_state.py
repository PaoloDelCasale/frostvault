"""Explicit Current Snapshot and in-flight replacement references.

Temporary integration revision; the parent will rechain and squash the baseline.
No old-catalog election is permitted: this revision is only for a fresh baseline.
"""
from alembic import op
import sqlalchemy as sa

revision = "0075_file_state"
down_revision = "0074_owner_singleton"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM archive_versions LIMIT 1")).first():
        raise RuntimeError("The new Current Snapshot baseline requires an empty catalog; no version election or adoption is supported")
    op.add_column("vault_files", sa.Column("current_version_id", sa.String(36)))
    op.add_column("vault_files", sa.Column("replacing_version_id", sa.String(36)))
    # The existing archive_versions_remediation_scope_uq unique index supplies
    # the exact (version, file, Vault) reference key; do not duplicate it.
    for column in ("current_version_id", "replacing_version_id"):
        op.create_foreign_key(f"vault_files_{column}_fk", "vault_files", "archive_versions",
                              [column, "id", "vault_id"], ["id", "vault_file_id", "vault_id"],
                              deferrable=True, initially="DEFERRED")
    op.create_check_constraint("vault_files_distinct_snapshot_ck", "vault_files",
                               "current_version_id IS DISTINCT FROM replacing_version_id OR current_version_id IS NULL")
    # Scope is a composite FK. Trust is checked when publishing the pointer;
    # later observations may report missing/unknown/mismatch without losing the
    # identity of the last verified snapshot. They must never elect a fallback.
    op.execute("""CREATE FUNCTION validate_current_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (TG_OP='INSERT' OR NEW.current_version_id IS DISTINCT FROM OLD.current_version_id)
             AND NEW.current_version_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM archive_versions av WHERE av.id=NEW.current_version_id
                 AND av.vault_file_id=NEW.id AND av.vault_id=NEW.vault_id
                 AND av.integrity='verified' AND av.availability='available'
                 AND av.plaintext_sha256 ~ '^[0-9a-f]{64}$' AND av.verified_at IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'Current Snapshot requires a verified available exact Archive Version' USING ERRCODE='23514';
          END IF;
          IF NEW.replacing_version_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM archive_versions av WHERE av.id=NEW.replacing_version_id
                AND av.availability='purged') THEN
            RAISE EXCEPTION 'A purged Archive Version cannot replace a snapshot' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER vault_files_current_snapshot_guard
        BEFORE INSERT OR UPDATE OF current_version_id,replacing_version_id ON vault_files
        FOR EACH ROW EXECUTE FUNCTION validate_current_snapshot()""")
    op.execute("""CREATE FUNCTION prevent_purged_snapshot_reference() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM vault_files vf JOIN archive_versions av
              ON av.id IN (vf.current_version_id,vf.replacing_version_id)
              WHERE vf.id=NEW.vault_file_id AND av.availability='purged') THEN
            RAISE EXCEPTION 'A purged Archive Version cannot be a snapshot reference' USING ERRCODE='23514';
          END IF;
          RETURN NULL;
        END $$""")
    op.execute("""CREATE CONSTRAINT TRIGGER archive_versions_snapshot_reference_guard
        AFTER INSERT OR UPDATE ON archive_versions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.availability='purged')
        EXECUTE FUNCTION prevent_purged_snapshot_reference()""")
    op.drop_index("archive_versions_file_verification_idx", table_name="archive_versions")
    op.drop_constraint("archive_versions_file_number_uq", "archive_versions", type_="unique")
    op.drop_column("archive_versions", "version_number")


def downgrade() -> None:
    raise RuntimeError("The incompatible Current Snapshot baseline cannot be downgraded")
