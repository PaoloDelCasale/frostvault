"""Billed Cost per Vault from the AWS Data Exports CUR 2.0 export (#738, ADR-0051).

``billing_export_status`` holds at most one row: the outcome of the last time
FrostVault looked for the export under ``billing/cur/`` in the System Bucket.
``checked_at`` is when that look started; the next one is due a day later
(six hours while no export was found), after a restart too. ``state``:

- ``checking``: the first look is running;
- ``not_configured``: nothing under the prefix;
- ``waiting``: the export's folder exists, its first Manifest.json does not;
- ``ok``: the newest export was read at ``read_at``;
- ``error``: the export could not be read; ``error`` says why (safe text).

The columns of the last successful read (``read_at``, ``manifest_key``,
``exported_at``: when AWS delivered that Manifest.json, ``data_through``: the
newest usage end in it, ``currency``) stay set after a later error.

``billed_costs`` holds the totals of that read, by billing period (first day of
the month, the export's ``BILLING_PERIOD``), bucket and category. A Vault
Bucket's rows name their Vault; the System Bucket and a shared bucket of older
Vaults are rows of the installation, never attributed to one Vault. Amounts are
in the billing currency, and can be negative (credits, refunds). A read
replaces every row at once. Nothing here calls AWS and nothing is backfilled.
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
    error text,
    CONSTRAINT billing_export_status_single_row_ck CHECK (id = 1),
    CONSTRAINT billing_export_status_state_ck CHECK (
        state IN ('checking', 'not_configured', 'waiting', 'ok', 'error')
    ),
    CONSTRAINT billing_export_status_error_ck CHECK (
        (state = 'error') = (error IS NOT NULL AND btrim(error) <> '')
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

CREATE TABLE billed_costs (
    billing_period date NOT NULL,
    bucket text COLLATE "C" NOT NULL,
    role text COLLATE "C" NOT NULL,
    vault_id bigint REFERENCES vaults(id) ON DELETE CASCADE,
    category text COLLATE "C" NOT NULL,
    amount numeric(24, 10) NOT NULL,
    PRIMARY KEY (billing_period, bucket, category),
    CONSTRAINT billed_costs_period_ck CHECK (
        billing_period = date_trunc('month', billing_period)::date
    ),
    CONSTRAINT billed_costs_role_ck CHECK (role IN ('vault', 'system', 'shared')),
    CONSTRAINT billed_costs_vault_ck CHECK ((role = 'vault') = (vault_id IS NOT NULL)),
    CONSTRAINT billed_costs_category_ck CHECK (
        category IN ('storage', 'requests', 'transitions', 'retrievals', 'data_transfer', 'other')
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
