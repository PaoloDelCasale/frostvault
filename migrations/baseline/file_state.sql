-- Canonical current-file component for the new incompatible baseline.
-- Not an incremental migration or a compatibility view. The complete baseline
-- must add scoped cloud/evidence references before the application cutover.
CREATE TABLE file_state (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vault_id bigint NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired','purged')),
    created_at timestamptz NOT NULL,
    retired_at timestamptz,
    current_path text,
    path_since timestamptz,
    path_generation uuid DEFAULT gen_random_uuid(),
    observation_generation uuid,
    local_presence text CHECK (local_presence IN ('present','missing','unsupported')),
    local_type text CHECK (local_type IN ('regular','directory','symlink','special')),
    local_size bigint CHECK (local_size >= 0),
    local_mtime_ns bigint,
    local_sha256 bytea,
    local_last_seen_at timestamptz,
    local_observed_at timestamptz,
    local_content_observed_at timestamptz,
    local_recovered_at timestamptz,
    CONSTRAINT file_state_scope_uq UNIQUE (id,vault_id),
    CONSTRAINT file_state_path_pair_ck CHECK (
        (current_path IS NULL AND path_since IS NULL AND path_generation IS NULL)
        OR (current_path IS NOT NULL AND path_since IS NOT NULL AND path_generation IS NOT NULL)
    ),
    CONSTRAINT file_state_active_path_ck CHECK (status <> 'active' OR current_path IS NOT NULL),
    CONSTRAINT file_state_relative_path_ck CHECK (
        current_path <> '' AND left(current_path,1) <> '/' AND right(current_path,1) <> '/'
        AND strpos(current_path,'//') = 0 AND current_path !~ '(^|/)\.{1,2}(/|$)'
    ),
    CONSTRAINT file_state_local_observation_ck CHECK (
        (observation_generation IS NULL AND local_presence IS NULL AND local_type IS NULL
         AND local_size IS NULL AND local_mtime_ns IS NULL AND local_sha256 IS NULL
         AND local_last_seen_at IS NULL AND local_observed_at IS NULL
         AND local_content_observed_at IS NULL AND local_recovered_at IS NULL)
        OR (observation_generation IS NOT NULL AND local_presence IS NOT NULL
            AND local_type IS NOT NULL AND local_last_seen_at IS NOT NULL
            AND local_observed_at IS NOT NULL AND local_content_observed_at IS NOT NULL)
    ),
    CONSTRAINT file_state_digest_ck CHECK (
        local_sha256 IS NULL OR (local_type='regular' AND octet_length(local_sha256)=32)
    )
);
CREATE UNIQUE INDEX file_state_current_path_uq ON file_state(vault_id,current_path)
    WHERE current_path IS NOT NULL;
CREATE INDEX file_state_path_prefix_idx ON file_state(vault_id,current_path text_pattern_ops)
    WHERE status='active';
CREATE INDEX file_state_presence_idx ON file_state(vault_id,local_presence) WHERE status='active';
CREATE INDEX file_state_fingerprint_idx ON file_state(vault_id,local_sha256)
    WHERE status='active' AND local_type='regular' AND local_sha256 IS NOT NULL;

-- Only closed mappings live here. Current path has one authority: file_state.
CREATE TABLE file_path_history (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vault_file_id uuid NOT NULL,
    vault_id bigint NOT NULL,
    path text NOT NULL,
    path_generation uuid NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz NOT NULL,
    CONSTRAINT file_path_history_scope_fk FOREIGN KEY (vault_file_id,vault_id)
        REFERENCES file_state(id,vault_id) ON DELETE CASCADE,
    CONSTRAINT file_path_history_generation_uq UNIQUE (vault_file_id,path_generation)
);
CREATE INDEX file_path_history_lookup_idx ON file_path_history(vault_id,path,vault_file_id);
