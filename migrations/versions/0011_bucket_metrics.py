"""Daily sizes of each Vault Bucket read from CloudWatch (#737, ADR-0051).

Once a day FrostVault reads, with CloudWatch ``GetMetricStatistics`` (never
``GetMetricData``), the S3 storage metrics of every dedicated Vault Bucket:
``BucketSizeBytes`` per ``StorageType`` and ``NumberOfObjects``.

``vault_bucket_metric_days`` keeps one row per Vault and UTC day CloudWatch
reported: the bytes of every reported StorageType (the CloudWatch dimension
value, e.g. ``DeepArchiveStorage`` or ``DeepArchiveObjectOverhead``) and the
object count, NULL when CloudWatch reported none for that day. Only the last
days are kept (the service prunes older rows).

``vault_bucket_metric_reads`` keeps the last read attempt of each Vault: when,
its outcome and an AWS error code (never a message). A restart reads it back,
so a Vault is never read again within a day of its last attempt.

Both start empty: nothing is backfilled and nothing here calls AWS.
"""
from __future__ import annotations

from alembic import op


revision = "0011_bucket_metrics"
down_revision = "0010_vault_buckets"
branch_labels = None
depends_on = None


SCHEMA = """
CREATE TABLE vault_bucket_metric_days (
    vault_id bigint NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    day date NOT NULL,
    storage_bytes jsonb NOT NULL,
    object_count bigint,
    read_at timestamptz NOT NULL,
    PRIMARY KEY (vault_id, day),
    CONSTRAINT vault_bucket_metric_days_bytes_ck
        CHECK (jsonb_typeof(storage_bytes) = 'object'),
    CONSTRAINT vault_bucket_metric_days_objects_ck
        CHECK (object_count IS NULL OR object_count >= 0)
);

CREATE TABLE vault_bucket_metric_reads (
    vault_id bigint PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    attempted_at timestamptz NOT NULL,
    -- ok: data points saved; no_data: CloudWatch has none yet (a new bucket);
    -- access_denied: no cloudwatch:GetMetricStatistics; failed: another error.
    outcome text COLLATE "C" NOT NULL,
    error_code text COLLATE "C",
    CONSTRAINT vault_bucket_metric_reads_outcome_ck
        CHECK (outcome IN ('ok', 'no_data', 'access_denied', 'failed')),
    CONSTRAINT vault_bucket_metric_reads_error_ck CHECK (
        (outcome IN ('access_denied', 'failed'))
            = (error_code IS NOT NULL AND btrim(error_code) <> '')
    )
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0011_bucket_metrics has no downgrade: restore a metadata backup instead"
    )
