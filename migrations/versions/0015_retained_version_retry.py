"""Retained Versions: batched sweep with visible failures (#749, ADR-0050).

The sweep of ``retained_versions`` claims a batch of due rows, releases the
database and deletes them with ``DeleteObjects``; it never holds a connection
while S3 answers. Two columns make that possible:

- ``next_attempt_at``: no attempt before this instant. Set when a batch is
  claimed (a crashed sweep lets the claim lapse) and after a failure: one hour
  for a transient S3 error, a day for any other. NULL: due as soon as
  ``retain_until`` has passed.
- ``failures``: consecutive failed deletions of the row; a deletion or a
  refusal explained by the Retention Lock resets it. After three the Vault
  gets a Problem.

``attempts``, ``last_attempt_at`` and ``last_error`` keep their meaning.
Nothing is backfilled: existing rows start with no failure and are due at
their ``retain_until``. No S3 call.
"""
from __future__ import annotations

from alembic import op


revision = "0015_retained_version_retry"
down_revision = "0014_vault_bucket_deletion"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE retained_versions
    ADD COLUMN next_attempt_at timestamptz,
    ADD COLUMN failures integer NOT NULL DEFAULT 0,
    ADD CONSTRAINT retained_versions_failures_ck CHECK (failures >= 0);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0015_retained_version_retry has no downgrade: restore a metadata backup instead"
    )
