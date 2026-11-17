"""Class Entry of each Archive Version and the duplicate cleanup's minimum option (#731).

``archive_versions.storage_class_since`` is the Class Entry: when the version
entered its current storage class. A version written straight into a class
(PUT or copy) enters it when it is created; afterwards the column changes only
when an observation (listing, audit, restore check) sees another class, and
then takes the instant of that observation. It is NULL when nothing was
recorded: every existing version, and one whose class was never known. No
value is backfilled; until one is recorded, FrostVault estimates the Class
Entry from ``created_at`` and the Vault's Storage Plan and says so.

``vaults.duplicate_cleanup_beyond_minimum``: Automatic Duplicate Cleanup
schedules only the Cloud Duplicates whose Minimum Storage Duration has ended
by the time they would be deleted. Off for every existing and new Vault.
"""
from __future__ import annotations

from alembic import op


revision = "0009_class_entry"
down_revision = "0008_system_bucket"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE archive_versions ADD COLUMN storage_class_since timestamptz;

ALTER TABLE vaults
    ADD COLUMN duplicate_cleanup_beyond_minimum boolean NOT NULL DEFAULT false;
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0009_class_entry has no downgrade: restore a metadata backup instead"
    )
