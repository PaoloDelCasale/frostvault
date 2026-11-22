"""The retired v1 Root Identity of a Vault, kept after its upgrade (#784, ADR-0054).

``vaults.legacy_root_identity`` holds the ``linux-stat-dir-v1`` identity a
Vault had when FrostVault proved, at startup, that its root is the same folder
and rewrote the identity as ``linux-stat-dir-v2``. Upload snapshots, cleanup
batches and other proofs written earlier pin the old value and stay immutable;
this column lets them still name the same root. It is NULL for every existing
Vault (nothing upgraded yet: no invented value) and for every Vault enrolled or
reconnected later, and is cleared whenever the root identity is replaced.
"""
from __future__ import annotations

from alembic import op


revision = "0018_legacy_root_identity"
down_revision = "0017_mirror_cleanup"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE vaults
    ADD COLUMN legacy_root_identity text,
    ADD CONSTRAINT vaults_legacy_root_identity_ck
        CHECK (legacy_root_identity IS NULL
               OR (root_identity IS NOT NULL AND legacy_root_identity <> root_identity));
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0018_legacy_root_identity has no downgrade: restore a metadata backup instead"
    )
