"""A dedicated Vault Bucket for every new Vault (#735, ADR-0049).

``vault_targets.key_layout`` says where a Vault's objects live in its target's
bucket, so every key is built from the target, never from configuration:

- ``vault_prefix``: under ``vaults/<vault uuid>/``. Every target that exists
  before this revision is one (the shared ``VAULT_S3_BUCKET`` bucket), so the
  column is filled with the layout those Vaults have always used; nothing in
  S3 is moved or renamed.
- ``bucket_root``: at the root of a dedicated ``frostvault-<slug>-<8 random>``
  bucket that FrostVault created for exactly one Vault.

The column is added with ``vault_prefix`` to fill the pre-existing targets,
then loses its default: every target inserted afterwards states its layout.
A ``bucket_root`` target belongs to one Vault only (``vaults_dedicated_target``).
No dedicated name starts with ``frostvault-system-``, the System Bucket's shape.
``vault_targets.retention_lock_days`` is the Governance Retention Lock a
dedicated bucket was created with (#736); NULL means none.

A new Vault is ``provisioning`` until its bucket is created, configured and
verified, then ``active``. Its target (the reserved bucket name) is saved
before CreateBucket, so an interrupted creation resumes on the same name.
While provisioning, and only then, the Vault may move to a new target: a name
owned by another AWS account is replaced. ``vault_bucket_provisioning`` holds
the progress and the last error of that creation, one row per provisioning
Vault, removed when the Vault becomes active. ``verified_at`` is when the
configuration was verified: the Vault becomes active 15 minutes later, the
time AWS recommends after enabling versioning before writing objects.
Nothing here calls S3.
"""
from __future__ import annotations

from alembic import op


revision = "0010_vault_buckets"
down_revision = "0009_class_entry"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vault_targets
    ADD COLUMN key_layout text COLLATE "C" NOT NULL DEFAULT 'vault_prefix',
    ADD COLUMN retention_lock_days integer;
ALTER TABLE vault_targets ALTER COLUMN key_layout DROP DEFAULT;
ALTER TABLE vault_targets
    ADD CONSTRAINT vault_targets_key_layout_ck
        CHECK (key_layout IN ('vault_prefix', 'bucket_root')),
    ADD CONSTRAINT vault_targets_dedicated_name_ck CHECK (
        key_layout <> 'bucket_root' OR (
            char_length(bucket) <= 63
            AND bucket ~ '^frostvault-[a-z0-9]+(-[a-z0-9]+)*-[a-z0-9]{8}$'
            AND bucket !~ '^frostvault-system-'
        )
    ),
    ADD CONSTRAINT vault_targets_retention_lock_ck CHECK (
        retention_lock_days IS NULL
        OR (key_layout = 'bucket_root' AND retention_lock_days BETWEEN 1 AND 36500)
    );

ALTER TABLE vaults DROP CONSTRAINT vaults_lifecycle_ck;
ALTER TABLE vaults ADD CONSTRAINT vaults_lifecycle_ck CHECK (
    lifecycle IN ('provisioning', 'active', 'reconnect_required', 'stopping', 'tombstone')
);

CREATE OR REPLACE FUNCTION vaults_identity_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.uuid IS DISTINCT FROM OLD.uuid THEN
        RAISE EXCEPTION 'a Vault keeps its namespace and target'
            USING ERRCODE = 'restrict_violation';
    END IF;
    -- Only a Vault whose bucket is still being created may take another
    -- reserved name; it stays provisioning while it does.
    IF NEW.target_id IS DISTINCT FROM OLD.target_id
       AND NOT (OLD.lifecycle = 'provisioning' AND NEW.lifecycle = 'provisioning') THEN
        RAISE EXCEPTION 'a Vault keeps its namespace and target'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.lifecycle = 'tombstone' AND NEW.lifecycle <> 'tombstone' THEN
        RAISE EXCEPTION 'a Vault Tombstone never resumes'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.lifecycle = 'provisioning' AND OLD.lifecycle <> 'provisioning' THEN
        RAISE EXCEPTION 'only a new Vault is provisioning'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;

-- A Vault Bucket holds exactly one Vault: no second Vault may take its target.
CREATE FUNCTION vaults_dedicated_target() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Lock the target so two concurrent inserts cannot both pass.
    PERFORM 1 FROM vault_targets
        WHERE id = NEW.target_id AND key_layout = 'bucket_root' FOR UPDATE;
    IF FOUND AND EXISTS (
        SELECT 1 FROM vaults WHERE target_id = NEW.target_id AND id <> NEW.id
    ) THEN
        RAISE EXCEPTION 'a Vault Bucket belongs to one Vault'
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER vaults_dedicated_target
    BEFORE INSERT OR UPDATE OF target_id ON vaults
    FOR EACH ROW EXECUTE FUNCTION vaults_dedicated_target();

CREATE TABLE vault_bucket_provisioning (
    vault_id bigint PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    requested_at timestamptz NOT NULL,
    -- reserved: name saved; created: CreateBucket answered (or the bucket was
    -- already ours); configured: the whole configuration was applied;
    -- verified: the verification passed at verified_at. The Vault becomes
    -- active, and this row goes, 15 minutes after verified_at.
    stage text COLLATE "C" NOT NULL DEFAULT 'reserved',
    attempts integer NOT NULL DEFAULT 0,
    last_attempt_at timestamptz,
    last_error text,
    verified_at timestamptz,
    CONSTRAINT vault_bucket_provisioning_stage_ck
        CHECK (stage IN ('reserved', 'created', 'configured', 'verified')),
    CONSTRAINT vault_bucket_provisioning_verified_ck
        CHECK ((stage = 'verified') = (verified_at IS NOT NULL)),
    CONSTRAINT vault_bucket_provisioning_attempts_ck CHECK (attempts >= 0),
    CONSTRAINT vault_bucket_provisioning_error_ck CHECK (
        last_error IS NULL OR (btrim(last_error) <> '' AND last_attempt_at IS NOT NULL)
    )
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0010_vault_buckets has no downgrade: restore a metadata backup instead"
    )
