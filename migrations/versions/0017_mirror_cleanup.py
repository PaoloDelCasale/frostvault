"""Mirror Cleanup, per Vault and off by default (#771, ADR-0052).

The Owner's settings live on the Vault, apart from Automatic Duplicate
Cleanup's: ``mirror_cleanup_enabled`` (off for every existing and new Vault),
the wait in days counted from the last sighting of the Local Copy (1-180),
the safety cap of one run in files and bytes, the optional folders the
cleanup is limited to (relative to the Vault root; empty = the whole Vault)
and whether it waits for the Minimum Storage Duration.
``mirror_cleanup_last_run_at`` and ``mirror_cleanup_last_outcome`` are NULL
until a first run happens, so a restart never runs early and an upgrade
invents no past run. The totals deleted so far are NULL until a first member
is deleted: no invented zero.

``cloud_purge_requests.mirror_cleanup`` marks an automatic Delayed Cloud
Deletion scheduled by the Mirror Cleanup; every existing request was the
Owner's or the Duplicate Cleanup's, so none is.
"""
from __future__ import annotations

from alembic import op


revision = "0017_mirror_cleanup"
down_revision = "0016_rename_missing_size_idx"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vaults
    ADD COLUMN mirror_cleanup_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN mirror_cleanup_grace_days integer NOT NULL DEFAULT 7,
    ADD COLUMN mirror_cleanup_cap_files integer NOT NULL DEFAULT 5000,
    ADD COLUMN mirror_cleanup_cap_bytes bigint NOT NULL DEFAULT 53687091200,
    ADD COLUMN mirror_cleanup_folders text[] NOT NULL DEFAULT '{}',
    ADD COLUMN mirror_cleanup_beyond_minimum boolean NOT NULL DEFAULT false,
    ADD COLUMN mirror_cleanup_last_run_at timestamptz,
    ADD COLUMN mirror_cleanup_last_outcome jsonb,
    ADD COLUMN mirror_cleanup_deleted_count bigint,
    ADD COLUMN mirror_cleanup_deleted_bytes bigint,
    ADD COLUMN mirror_cleanup_last_deleted_at timestamptz,
    ADD CONSTRAINT vaults_mirror_cleanup_grace_ck
        CHECK (mirror_cleanup_grace_days BETWEEN 1 AND 180),
    ADD CONSTRAINT vaults_mirror_cleanup_cap_ck
        CHECK (mirror_cleanup_cap_files >= 1 AND mirror_cleanup_cap_bytes >= 1),
    ADD CONSTRAINT vaults_mirror_cleanup_folders_ck
        CHECK (cardinality(mirror_cleanup_folders) <= 100
               AND array_position(mirror_cleanup_folders, NULL) IS NULL
               AND array_position(mirror_cleanup_folders, '') IS NULL),
    ADD CONSTRAINT vaults_mirror_cleanup_outcome_ck
        CHECK (mirror_cleanup_last_outcome IS NULL
               OR jsonb_typeof(mirror_cleanup_last_outcome) = 'object'),
    ADD CONSTRAINT vaults_mirror_cleanup_totals_ck
        CHECK ((mirror_cleanup_deleted_count IS NULL) = (mirror_cleanup_deleted_bytes IS NULL)
               AND (mirror_cleanup_deleted_count IS NULL) = (mirror_cleanup_last_deleted_at IS NULL)
               AND COALESCE(mirror_cleanup_deleted_count, 0) >= 0
               AND COALESCE(mirror_cleanup_deleted_bytes, 0) >= 0);

ALTER TABLE cloud_purge_requests
    ADD COLUMN mirror_cleanup boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT cloud_purge_requests_mirror_cleanup_ck
        CHECK (NOT mirror_cleanup OR automatic);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0017_mirror_cleanup has no downgrade: restore a metadata backup instead"
    )
