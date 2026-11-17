"""Vault Deletion: optional deletion of the empty Vault Bucket (#739).

``vault_deletions.delete_bucket``: the Owner ticked «also delete the bucket»
when confirming the Vault Deletion of a Vault with its own Vault Bucket
(``key_layout = 'bucket_root'``). Never true for a Vault in the shared bucket,
which is never deleted. ``bucket_deleted_at``: when DeleteBucket succeeded;
from then on nothing reads the bucket (CloudWatch reads skip the Vault).
Nothing is backfilled: an older request keeps its bucket.
"""
from __future__ import annotations

from alembic import op


revision = "0014_vault_bucket_deletion"
down_revision = "0013_retention_lock"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vault_deletions
    ADD COLUMN delete_bucket boolean NOT NULL DEFAULT false,
    ADD COLUMN bucket_deleted_at timestamptz,
    ADD CONSTRAINT vault_deletions_bucket_deleted_ck CHECK (
        bucket_deleted_at IS NULL OR delete_bucket
    );
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0014_vault_bucket_deletion has no downgrade: restore a metadata backup instead"
    )
