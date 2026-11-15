"""The Local Copy observation an automatic upload was admitted on (#689).

While a full scan of a Vault verifies fingerprints it no longer holds the
Vault scan lock, so automatic upload admission runs meanwhile. An upload
admitted then records the generation of the File it was admitted on. If the
scan later compensates that observation (its journal is rolled back), the
generation changes and the Job is cancelled when it is claimed, before any
read or send; automatic admission decides again on the current observation.

NULL: not recorded (admitted outside a running scan, manual, or before this
revision). Nothing is backfilled.
"""
from __future__ import annotations

from alembic import op


revision = "0003_job_admitted_generation"
down_revision = "0002_file_ownership"
branch_labels = None
depends_on = None


SCHEMA = """
ALTER TABLE jobs
    ADD COLUMN admitted_generation bigint,
    ADD CONSTRAINT jobs_admitted_generation_ck CHECK (
        admitted_generation IS NULL
        OR (action = 'upload' AND origin = 'automatic' AND admitted_generation >= 0)
    );
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0003_job_admitted_generation has no downgrade: restore a metadata backup instead"
    )
