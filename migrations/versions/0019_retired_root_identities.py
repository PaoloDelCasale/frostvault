"""Every retired Root Identity of a Vault, not only the last one (#787, ADR-0055).

``vaults.retired_root_identities`` replaces ``vaults.legacy_root_identity``
(0018). The startup upgrade rewrites a Vault's identity for the same folder
(v1 -> v2 since #784, v1/v2 -> v3 since #787) and appends the replaced value:
upload snapshots, upload attempts, cleanup batches and other immutable proofs
pinned with any earlier value still name the current root. A single column
lost the v1 value of a Vault upgraded twice.

The existing ``legacy_root_identity`` value, when present, becomes the only
element; every other Vault starts with an empty set. Nothing else is
invented. Enrollment, Reconnect Folder and a metadata restore empty it.
"""
from __future__ import annotations

from alembic import op


revision = "0019_retired_root_identities"
down_revision = "0018_legacy_root_identity"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vaults
    ADD COLUMN retired_root_identities text[] NOT NULL DEFAULT '{}';
UPDATE vaults SET retired_root_identities = ARRAY[legacy_root_identity]
    WHERE legacy_root_identity IS NOT NULL;
ALTER TABLE vaults
    DROP CONSTRAINT vaults_legacy_root_identity_ck,
    DROP COLUMN legacy_root_identity,
    ADD CONSTRAINT vaults_retired_root_identities_ck
        CHECK (array_position(retired_root_identities, NULL) IS NULL
               AND (cardinality(retired_root_identities) = 0
                    OR (root_identity IS NOT NULL
                        AND array_position(retired_root_identities, root_identity) IS NULL)));
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0019_retired_root_identities has no downgrade: restore a metadata backup instead"
    )
