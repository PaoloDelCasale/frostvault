"""Billed Cost per Vault from the AWS Data Exports CUR 2.0 export (#738, ADR-0051).

``billing_export_status`` holds at most one row: the outcome of the last time
FrostVault looked for the export under ``billing/cur/`` in the System Bucket.
``checked_at`` is when that look started; the next one is due a day later, or
six hours later while no export was found or after a transient error, after a
restart too. ``state``:

- ``checking``: the first look is running;
- ``not_configured``: nothing under the prefix;
- ``waiting``: the export's folder exists, its first manifest does not;
- ``ok``: the newest export was read at ``read_at``;
- ``error``: the export could not be read; ``error_code`` says why (a code the
  UI translates) and ``error_detail`` may name an S3 action and AWS error code,
  columns or currencies, never a provider message.

The columns of the last successful read (``read_at``, ``manifest_key``,
``exported_at``: when AWS delivered that manifest, ``data_through``: the newest
usage end in it, ``currency``) stay set after a later error.

``billed_cost_periods`` holds, per billing period read (first day of the
month, the export's ``BILLING_PERIOD``), the manifest its totals came from
(key, ETag, LastModified) and a digest of the buckets FrostVault knew: while
both are unchanged the period is not read again.

``billed_costs`` holds the totals of those periods by bucket and category. A
Vault Bucket's rows name their Vault; the System Bucket and a shared bucket of
older Vaults are rows of the installation, never attributed to one Vault.
Amounts are in the billing currency and can be negative (``credits``: credits,
refunds and discounts). Nothing here calls AWS and nothing is backfilled.
"""
from __future__ import annotations

from alembic import op


revision = "0012_billed_cost"
down_revision = "0011_bucket_metrics"
branch_labels = None
depends_on = None


SCHEMA = """
CREATE TABLE billing_export_status (
    id smallint PRIMARY KEY DEFAULT 1,
    state text COLLATE "C" NOT NULL,
    checked_at timestamptz NOT NULL,
    bucket text COLLATE "C",
    read_at timestamptz,
    manifest_key text COLLATE "C",
    exported_at timestamptz,
    data_through timestamptz,
    currency text COLLATE "C",
    error_code text COLLATE "C",
    error_detail text,
    CONSTRAINT billing_export_status_single_row_ck CHECK (id = 1),
    CONSTRAINT billing_export_status_state_ck CHECK (
        state IN ('checking', 'not_configured', 'waiting', 'ok', 'error')
    ),
    CONSTRAINT billing_export_status_error_ck CHECK (
        (state = 'error') = (error_code IS NOT NULL AND error_code ~ '^[a-z0-9_]{1,64}$')
    ),
    CONSTRAINT billing_export_status_detail_ck CHECK (
        error_detail IS NULL OR (state = 'error' AND length(error_detail) <= 200)
    ),
    CONSTRAINT billing_export_status_read_ck CHECK (
        state <> 'ok' OR (
            read_at IS NOT NULL AND manifest_key IS NOT NULL
            AND exported_at IS NOT NULL AND currency IS NOT NULL AND bucket IS NOT NULL
        )
    ),
    CONSTRAINT billing_export_status_currency_ck CHECK (
        currency IS NULL OR currency ~ '^[A-Z]{3}$'
    )
);

CREATE TABLE billed_cost_periods (
    billing_period date PRIMARY KEY,
    manifest_key text COLLATE "C" NOT NULL,
    manifest_etag text COLLATE "C" NOT NULL,
    manifest_modified timestamptz NOT NULL,
    roles_digest text COLLATE "C" NOT NULL,
    currency text COLLATE "C",
    data_through timestamptz,
    read_at timestamptz NOT NULL,
    CONSTRAINT billed_cost_periods_period_ck CHECK (
        billing_period = date_trunc('month', billing_period)::date
    ),
    CONSTRAINT billed_cost_periods_currency_ck CHECK (
        currency IS NULL OR currency ~ '^[A-Z]{3}$'
    )
);

CREATE TABLE billed_costs (
    billing_period date NOT NULL REFERENCES billed_cost_periods(billing_period) ON DELETE CASCADE,
    bucket text COLLATE "C" NOT NULL,
    role text COLLATE "C" NOT NULL,
    vault_id bigint REFERENCES vaults(id) ON DELETE CASCADE,
    category text COLLATE "C" NOT NULL,
    amount numeric(24, 10) NOT NULL,
    PRIMARY KEY (billing_period, bucket, category),
    CONSTRAINT billed_costs_role_ck CHECK (role IN ('vault', 'system', 'shared')),
    CONSTRAINT billed_costs_vault_ck CHECK ((role = 'vault') = (vault_id IS NOT NULL)),
    CONSTRAINT billed_costs_category_ck CHECK (
        category IN ('storage', 'requests', 'transitions', 'retrievals', 'data_transfer', 'other',
                     'credits')
    )
);

CREATE INDEX billed_costs_vault_idx ON billed_costs (vault_id) WHERE vault_id IS NOT NULL;
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    raise RuntimeError(
        "0012_billed_cost has no downgrade: restore a metadata backup instead"
    )
