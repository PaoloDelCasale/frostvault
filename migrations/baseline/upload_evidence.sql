-- Immutable expected evidence, independent receipts and explicit snapshot refs.
-- Canonical baseline component; runtime cutover/job/effect integration pending.
CREATE FUNCTION baseline_sha256_parts(parts bytea[]) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT array_ndims(parts)=1 AND array_lower(parts,1)=1 AND cardinality(parts) BETWEEN 1 AND 10000
        AND NOT EXISTS (SELECT 1 FROM unnest(parts) p WHERE p IS NULL OR octet_length(p)<>32)
$$;
CREATE TABLE upload_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vault_file_id uuid NOT NULL REFERENCES file_state(id) ON DELETE RESTRICT,
    evidence_hash bytea NOT NULL CHECK (octet_length(evidence_hash)=32),
    size bigint NOT NULL CHECK (size BETWEEN 0 AND 5497558138880),
    plaintext_sha256 bytea NOT NULL CHECK (octet_length(plaintext_sha256)=32),
    mtime_ns bigint NOT NULL,
    checksum_type text NOT NULL CHECK (checksum_type IN ('FULL_OBJECT','COMPOSITE')),
    checksum_sha256 text NOT NULL,
    part_size bigint NOT NULL CHECK (part_size BETWEEN 8388608 AND 1073741824),
    part_checksums bytea[] NOT NULL CHECK (baseline_sha256_parts(part_checksums)),
    created_at timestamptz NOT NULL,
    UNIQUE (id,vault_file_id),
    UNIQUE (vault_file_id,evidence_hash)
);
CREATE TABLE upload_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vault_file_id uuid NOT NULL REFERENCES file_state(id) ON DELETE RESTRICT,
    snapshot_id uuid NOT NULL,
    token uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    provider text NOT NULL CHECK (provider<>''),
    bucket text NOT NULL CHECK (bucket<>''),
    object_key text NOT NULL CHECK (object_key<>''),
    path_generation uuid NOT NULL,
    source_root text NOT NULL,
    root_identity text NOT NULL CHECK (root_identity<>''),
    root_identity_version text NOT NULL CHECK (root_identity_version<>''),
    -- Frozen CAS token, NOT a liveness reference to a historical object.
    -- An FK here would retain the entire chain of obsolete keepers forever.
    expected_current_id uuid,
    verification_mode text NOT NULL CHECK (verification_mode IN ('s3_checksum','read_back')),
    storage_class text NOT NULL DEFAULT 'STANDARD',
    created_at timestamptz NOT NULL,
    send_started_at timestamptz,
    settled_at timestamptz,
    FOREIGN KEY (snapshot_id,vault_file_id) REFERENCES upload_snapshots(id,vault_file_id) ON DELETE RESTRICT,
    UNIQUE (id,vault_file_id,provider,bucket,object_key)
);
-- Multiple contradictory acknowledgments MUST remain representable. Deduplicate
-- identical facts, never UNIQUE(attempt_id), never choose one by time/order.
CREATE TABLE upload_receipt_facts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id uuid NOT NULL REFERENCES upload_attempts(id) ON DELETE RESTRICT,
    receipt_hash bytea NOT NULL CHECK (octet_length(receipt_hash)=32),
    -- Malformed acknowledgments are still durable facts, not discarded before
    -- reconciliation. Only materialization requires a usable exact VersionId.
    provider_version_id text CHECK (provider_version_id IS NULL OR provider_version_id NOT IN ('','null')),
    response jsonb NOT NULL CHECK (jsonb_typeof(response)='object'
        AND (provider_version_id IS NULL OR (jsonb_typeof(response->'VersionId')='string'
            AND (response->>'VersionId') IS NOT DISTINCT FROM provider_version_id))
        AND (response-ARRAY['VersionId','ETag','ChecksumSHA256','ChecksumType','ContentLength','StorageClass'])='{}'::jsonb),
    received_at timestamptz NOT NULL,
    UNIQUE (attempt_id,receipt_hash),
    UNIQUE (id,attempt_id,provider_version_id)
);
CREATE TABLE archive_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vault_file_id uuid NOT NULL REFERENCES file_state(id) ON DELETE RESTRICT,
    attempt_id uuid NOT NULL,
    receipt_id uuid NOT NULL UNIQUE,
    provider text NOT NULL,
    bucket text NOT NULL,
    object_key text NOT NULL,
    provider_version_id text NOT NULL CHECK (provider_version_id NOT IN ('','null')),
    integrity text NOT NULL DEFAULT 'unverified' CHECK (integrity IN ('unverified','verified','mismatch')),
    availability text NOT NULL DEFAULT 'available' CHECK (availability IN ('available','missing','unknown','purged')),
    storage_class text NOT NULL,
    storage_class_source text NOT NULL DEFAULT 'upload' CHECK (storage_class_source IN ('upload','discovered','manual','unknown')),
    verification_method text CHECK (verification_method IN ('s3_checksum','read_back')),
    verification_fallback_reason text,
    verification_egress_bytes bigint CHECK (verification_egress_bytes>=0),
    verified_sha256 bytea CHECK (octet_length(verified_sha256)=32),
    verified_at timestamptz,
    FOREIGN KEY (attempt_id,vault_file_id,provider,bucket,object_key)
        REFERENCES upload_attempts(id,vault_file_id,provider,bucket,object_key) ON DELETE RESTRICT,
    FOREIGN KEY (receipt_id,attempt_id,provider_version_id)
        REFERENCES upload_receipt_facts(id,attempt_id,provider_version_id) ON DELETE RESTRICT,
    UNIQUE (provider,bucket,object_key,provider_version_id),
    UNIQUE (id,vault_file_id),
    CHECK (integrity<>'verified' OR
        (verification_method IS NOT NULL AND verified_sha256 IS NOT NULL AND verified_at IS NOT NULL))
);
ALTER TABLE file_state ADD COLUMN current_snapshot_id uuid;
ALTER TABLE file_state ADD COLUMN pending_snapshot_id uuid;
ALTER TABLE file_state ADD COLUMN matched_snapshot_id uuid;
ALTER TABLE file_state ADD COLUMN held_snapshot_id uuid;
ALTER TABLE file_state ADD CONSTRAINT file_state_current_scope_fk
    FOREIGN KEY (current_snapshot_id,id) REFERENCES archive_objects(id,vault_file_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE file_state ADD CONSTRAINT file_state_pending_scope_fk
    FOREIGN KEY (pending_snapshot_id,id) REFERENCES archive_objects(id,vault_file_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE file_state ADD CONSTRAINT file_state_matched_scope_fk
    FOREIGN KEY (matched_snapshot_id,id) REFERENCES archive_objects(id,vault_file_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE file_state ADD CONSTRAINT file_state_held_scope_fk
    FOREIGN KEY (held_snapshot_id,id) REFERENCES archive_objects(id,vault_file_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE file_state ADD CONSTRAINT file_state_distinct_snapshots_ck CHECK (
    current_snapshot_id IS DISTINCT FROM pending_snapshot_id OR current_snapshot_id IS NULL);
CREATE UNIQUE INDEX upload_attempts_one_open_file_uq ON upload_attempts(vault_file_id) WHERE settled_at IS NULL;
CREATE INDEX archive_objects_live_file_idx ON archive_objects(vault_file_id,id) WHERE availability<>'purged';

CREATE FUNCTION baseline_frozen_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Immutable expected evidence and receipt facts cannot be updated' USING ERRCODE='23514';
END $$;
CREATE TRIGGER upload_snapshots_immutable BEFORE UPDATE ON upload_snapshots
    FOR EACH ROW EXECUTE FUNCTION baseline_frozen_evidence();
CREATE TRIGGER upload_receipt_facts_immutable BEFORE UPDATE ON upload_receipt_facts
    FOR EACH ROW EXECUTE FUNCTION baseline_frozen_evidence();
CREATE FUNCTION baseline_attempt_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW)-ARRAY['send_started_at','settled_at']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['send_started_at','settled_at'])
       OR (OLD.send_started_at IS NOT NULL AND NEW.send_started_at IS DISTINCT FROM OLD.send_started_at)
       OR (OLD.settled_at IS NOT NULL AND
           (NEW.settled_at,NEW.send_started_at) IS DISTINCT FROM (OLD.settled_at,OLD.send_started_at)) THEN
        RAISE EXCEPTION 'Upload attempt identity and prior facts are immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER upload_attempts_immutable BEFORE UPDATE ON upload_attempts
    FOR EACH ROW EXECUTE FUNCTION baseline_attempt_transition();
CREATE FUNCTION baseline_attempt_retention() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.settled_at IS NULL THEN
        RAISE EXCEPTION 'An open upload attempt is required evidence' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER upload_attempts_required BEFORE DELETE ON upload_attempts
    FOR EACH ROW EXECUTE FUNCTION baseline_attempt_retention();
CREATE FUNCTION baseline_receipt_retention() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM upload_attempts WHERE id=OLD.attempt_id AND settled_at IS NULL) THEN
        RAISE EXCEPTION 'An open upload requires its receipt' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER upload_receipt_facts_required BEFORE DELETE ON upload_receipt_facts
    FOR EACH ROW EXECUTE FUNCTION baseline_receipt_retention();
CREATE FUNCTION baseline_archive_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.vault_file_id,NEW.attempt_id,NEW.receipt_id,NEW.provider,NEW.bucket,NEW.object_key,NEW.provider_version_id)
       IS DISTINCT FROM
       (OLD.id,OLD.vault_file_id,OLD.attempt_id,OLD.receipt_id,OLD.provider,OLD.bucket,OLD.object_key,OLD.provider_version_id)
       OR (OLD.verified_at IS NOT NULL AND
           (NEW.verification_method,NEW.verified_sha256,NEW.verified_at,NEW.verification_fallback_reason,NEW.verification_egress_bytes) IS DISTINCT FROM
           (OLD.verification_method,OLD.verified_sha256,OLD.verified_at,OLD.verification_fallback_reason,OLD.verification_egress_bytes)) THEN
        RAISE EXCEPTION 'Archive identity and committed proof are immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER archive_objects_identity BEFORE UPDATE ON archive_objects
    FOR EACH ROW EXECUTE FUNCTION baseline_archive_identity();
CREATE FUNCTION baseline_snapshot_reference() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP='INSERT' OR NEW.current_snapshot_id IS DISTINCT FROM OLD.current_snapshot_id)
       AND NEW.current_snapshot_id IS NOT NULL AND (NEW.status<>'active' OR NOT EXISTS (
        SELECT 1 FROM archive_objects a JOIN upload_attempts t ON t.id=a.attempt_id
        JOIN upload_snapshots s ON s.id=t.snapshot_id
        WHERE a.id=NEW.current_snapshot_id AND a.vault_file_id=NEW.id
          AND a.integrity='verified' AND a.availability='available'
          AND a.verified_sha256=s.plaintext_sha256 AND a.verified_at IS NOT NULL
          AND t.send_started_at IS NOT NULL AND t.path_generation=NEW.path_generation
          AND t.expected_current_id IS NOT DISTINCT FROM OLD.current_snapshot_id
          AND (SELECT count(*) FROM upload_receipt_facts r WHERE r.attempt_id=t.id)=1
    )) THEN
        RAISE EXCEPTION 'Current Snapshot requires exact verified scoped evidence' USING ERRCODE='23514';
    END IF;
    IF NEW.pending_snapshot_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM archive_objects WHERE id=NEW.pending_snapshot_id AND availability='purged'
    ) THEN
        RAISE EXCEPTION 'Purged object cannot be a pending snapshot' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_state_snapshot_reference BEFORE INSERT OR UPDATE OF current_snapshot_id,pending_snapshot_id ON file_state
    FOR EACH ROW EXECUTE FUNCTION baseline_snapshot_reference();
CREATE FUNCTION baseline_no_purged_reference() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM file_state f JOIN archive_objects a
        ON a.id IN (f.current_snapshot_id,f.pending_snapshot_id)
        WHERE f.id=NEW.vault_file_id AND a.availability='purged') THEN
        RAISE EXCEPTION 'Referenced snapshot cannot be purged' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER archive_objects_no_purged_reference AFTER INSERT OR UPDATE ON archive_objects
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.availability='purged')
    EXECUTE FUNCTION baseline_no_purged_reference();
