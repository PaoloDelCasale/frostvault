"""Automatic Duplicate Cleanup, per Vault and off by default (#726, ADR-0048).

The Owner's settings live on the Vault: ``duplicate_cleanup_enabled`` (off for
every existing and new Vault), the grace period in days counted from the last
sighting of the Local Copy, and the safety cap of one run in files and bytes.
``duplicate_cleanup_last_run_at`` is NULL until a first run happens, so a
restart never runs early and an upgrade invents no past run;
``duplicate_cleanup_last_outcome`` is NULL until then too.

``cloud_purge_requests.automatic`` marks a Delayed Cloud Deletion scheduled by
the cleanup instead of the Owner: its members are revoked one by one when they
stop being Cloud Duplicates, while a manual request stays all-or-nothing.
Every existing request was the Owner's, so it is not automatic.
"""
from __future__ import annotations

from alembic import op


revision = "0006_duplicate_cleanup"
down_revision = "0005_vault_deletions"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vaults
    ADD COLUMN duplicate_cleanup_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN duplicate_cleanup_grace_days integer NOT NULL DEFAULT 7,
    ADD COLUMN duplicate_cleanup_cap_files integer NOT NULL DEFAULT 5000,
    ADD COLUMN duplicate_cleanup_cap_bytes bigint NOT NULL DEFAULT 53687091200,
    ADD COLUMN duplicate_cleanup_last_run_at timestamptz,
    ADD COLUMN duplicate_cleanup_last_outcome jsonb,
    ADD CONSTRAINT vaults_duplicate_cleanup_grace_ck
        CHECK (duplicate_cleanup_grace_days BETWEEN 1 AND 90),
    ADD CONSTRAINT vaults_duplicate_cleanup_cap_ck
        CHECK (duplicate_cleanup_cap_files >= 1 AND duplicate_cleanup_cap_bytes >= 1),
    ADD CONSTRAINT vaults_duplicate_cleanup_outcome_ck
        CHECK (duplicate_cleanup_last_outcome IS NULL
               OR jsonb_typeof(duplicate_cleanup_last_outcome) = 'object');

ALTER TABLE cloud_purge_requests
    ADD COLUMN automatic boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT cloud_purge_requests_automatic_ck
        CHECK (NOT (automatic AND stop_protection));
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0006_duplicate_cleanup has no downgrade: restore a metadata backup instead"
    )
