"""Persist catalog revisions and events for event-driven SPA invalidation.

Revision ID: 0036_catalog_events
Revises: 0035_upload_verification_digest

The tables in this migration are derived projections. The canonical Vault File
and Archive Version tables remain authoritative; no existing catalog rows are
rewritten or backfilled here.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0036_catalog_events"
down_revision: str | None = "0035_upload_verification_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MAX_EVENT_DOMAIN_LENGTH = 64
MAX_EVENT_SCOPE_LENGTH = 512
MAX_EVENT_PAYLOAD_LENGTH = 4096


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # One row is the lock and durable high-water mark for each Vault. The
    # retained-from marker survives event pruning so readers can distinguish a
    # normal empty page from a retention gap.
    op.create_table(
        "vault_catalog_revisions",
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "revision",
            identifier,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "retained_from_revision",
            identifier,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="vault_catalog_revisions_revision_ck"),
        sa.CheckConstraint(
            "retained_from_revision >= 1 AND "
            "retained_from_revision <= revision + 1",
            name="vault_catalog_revisions_retention_ck",
        ),
    )

    op.create_table(
        "catalog_events",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", identifier, nullable=False),
        sa.Column("domain", sa.String(length=MAX_EVENT_DOMAIN_LENGTH), nullable=False),
        sa.Column(
            "scope",
            sa.String(length=MAX_EVENT_SCOPE_LENGTH),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "payload_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("revision > 0", name="catalog_events_revision_ck"),
        sa.CheckConstraint(
            f"length(trim(domain)) BETWEEN 1 AND {MAX_EVENT_DOMAIN_LENGTH}",
            name="catalog_events_domain_ck",
        ),
        sa.CheckConstraint(
            f"length(scope) <= {MAX_EVENT_SCOPE_LENGTH}",
            name="catalog_events_scope_ck",
        ),
        sa.CheckConstraint(
            f"length(payload_json) <= {MAX_EVENT_PAYLOAD_LENGTH}",
            name="catalog_events_payload_ck",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "revision",
            name="catalog_events_vault_revision_uq",
        ),
    )
    op.create_index(
        "catalog_events_vault_revision_idx",
        "catalog_events",
        ["vault_id", "revision"],
    )


def _table_has_rows(table: str) -> bool:
    connection = op.get_bind()
    row = connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
    return row is not None


def downgrade() -> None:
    # The event journal is part of reconnect correctness. Never silently destroy
    # durable history on a rollback.
    durable_tables = (
        "catalog_events",
        "vault_catalog_revisions",
    )
    if any(_table_has_rows(table) for table in durable_tables):
        raise RuntimeError(
            "Cannot downgrade 0036 after catalog event data has been persisted; "
            "export or explicitly clear derived history first"
        )

    op.drop_index("catalog_events_vault_revision_idx", table_name="catalog_events")
    op.drop_table("catalog_events")
    op.drop_table("vault_catalog_revisions")
