"""Persist catalog revisions/events and bounded filesystem health snapshots.

Revision ID: 0036_catalog_events_health
Revises: 0035_upload_verification_digest

The tables in this migration are derived projections.  The canonical Vault File
and Archive Version tables remain authoritative; no existing catalog rows are
rewritten or backfilled here.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0036_catalog_events_health"
down_revision: str | None = "0035_upload_verification_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MAX_EVENT_DOMAIN_LENGTH = 64
MAX_EVENT_SCOPE_LENGTH = 512
MAX_EVENT_PAYLOAD_LENGTH = 4096
MAX_HEALTH_SUMMARY_LENGTH = 16384
MAX_HEALTH_FINDINGS_PER_SNAPSHOT = 256
MAX_HEALTH_CODE_LENGTH = 80
MAX_HEALTH_SCOPE_LENGTH = 512
MAX_HEALTH_PATH_LENGTH = 1024
MAX_HEALTH_MESSAGE_LENGTH = 2048
MAX_HEALTH_REMEDIATION_LENGTH = 2048
MAX_HEALTH_ERROR_CODE_LENGTH = 80
MAX_HEALTH_ERROR_LENGTH = 500


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # One row is the lock and durable high-water mark for each Vault.  The
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

    op.create_table(
        "filesystem_health_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("catalog_revision", identifier, nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="checking",
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.Text()),
        sa.Column(
            "total_findings",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "sampled_findings",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "findings_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "summary_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("uid", sa.Integer()),
        sa.Column("gid", sa.Integer()),
        sa.Column(
            "error_code",
            sa.String(length=MAX_HEALTH_ERROR_CODE_LENGTH),
        ),
        sa.Column("error_message", sa.Text()),
        sa.CheckConstraint(
            "catalog_revision >= 0",
            name="filesystem_health_snapshots_revision_ck",
        ),
        sa.CheckConstraint(
            "status IN ('checking', 'current', 'stale', 'failed')",
            name="filesystem_health_snapshots_status_ck",
        ),
        sa.CheckConstraint(
            "total_findings >= 0 AND sampled_findings >= 0 AND "
            "sampled_findings <= total_findings AND "
            f"sampled_findings <= {MAX_HEALTH_FINDINGS_PER_SNAPSHOT}",
            name="filesystem_health_snapshots_counts_ck",
        ),
        sa.CheckConstraint(
            f"length(summary_json) <= {MAX_HEALTH_SUMMARY_LENGTH}",
            name="filesystem_health_snapshots_summary_ck",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR length(error_code) <= {MAX_HEALTH_ERROR_CODE_LENGTH}",
            name="filesystem_health_snapshots_error_code_ck",
        ),
        sa.CheckConstraint(
            f"error_message IS NULL OR length(error_message) <= {MAX_HEALTH_ERROR_LENGTH}",
            name="filesystem_health_snapshots_error_message_ck",
        ),
        sa.CheckConstraint(
            "uid IS NULL OR uid >= 0",
            name="filesystem_health_snapshots_uid_ck",
        ),
        sa.CheckConstraint(
            "gid IS NULL OR gid >= 0",
            name="filesystem_health_snapshots_gid_ck",
        ),
    )
    op.create_index(
        "filesystem_health_snapshots_vault_created_idx",
        "filesystem_health_snapshots",
        ["vault_id", "created_at"],
    )
    op.create_index(
        "filesystem_health_snapshots_vault_revision_idx",
        "filesystem_health_snapshots",
        ["vault_id", "catalog_revision"],
    )

    op.create_table(
        "filesystem_health_findings",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "snapshot_id",
            sa.String(length=36),
            sa.ForeignKey("filesystem_health_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=MAX_HEALTH_CODE_LENGTH), nullable=False),
        sa.Column(
            "scope",
            sa.String(length=MAX_HEALTH_SCOPE_LENGTH),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "path",
            sa.String(length=MAX_HEALTH_PATH_LENGTH),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "message",
            sa.String(length=MAX_HEALTH_MESSAGE_LENGTH),
            nullable=False,
        ),
        sa.Column(
            "remediation",
            sa.String(length=MAX_HEALTH_REMEDIATION_LENGTH),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "occurrences",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.CheckConstraint(
            f"length(trim(code)) BETWEEN 1 AND {MAX_HEALTH_CODE_LENGTH}",
            name="filesystem_health_findings_code_ck",
        ),
        sa.CheckConstraint(
            f"length(scope) <= {MAX_HEALTH_SCOPE_LENGTH}",
            name="filesystem_health_findings_scope_ck",
        ),
        sa.CheckConstraint(
            f"length(path) <= {MAX_HEALTH_PATH_LENGTH}",
            name="filesystem_health_findings_path_ck",
        ),
        sa.CheckConstraint(
            f"length(message) <= {MAX_HEALTH_MESSAGE_LENGTH}",
            name="filesystem_health_findings_message_ck",
        ),
        sa.CheckConstraint(
            f"length(remediation) <= {MAX_HEALTH_REMEDIATION_LENGTH}",
            name="filesystem_health_findings_remediation_ck",
        ),
        sa.CheckConstraint(
            "occurrences >= 1",
            name="filesystem_health_findings_occurrences_ck",
        ),
    )
    op.create_index(
        "filesystem_health_findings_snapshot_idx",
        "filesystem_health_findings",
        ["snapshot_id", "id"],
    )
    op.create_index(
        "filesystem_health_findings_code_idx",
        "filesystem_health_findings",
        ["snapshot_id", "code", "scope"],
    )


def _table_has_rows(table: str) -> bool:
    connection = op.get_bind()
    row = connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
    return row is not None


def downgrade() -> None:
    # These tables are derived, but an event journal and health cache are still
    # part of restart/reconnect correctness.  Never silently destroy them on a
    # rollback; an operator can explicitly retain the data and migrate forward.
    durable_tables = (
        "catalog_events",
        "vault_catalog_revisions",
        "filesystem_health_snapshots",
        "filesystem_health_findings",
    )
    if any(_table_has_rows(table) for table in durable_tables):
        raise RuntimeError(
            "Cannot downgrade 0036 after catalog event or filesystem health data "
            "has been persisted; export or explicitly clear derived history first"
        )

    op.drop_index(
        "filesystem_health_findings_code_idx",
        table_name="filesystem_health_findings",
    )
    op.drop_index(
        "filesystem_health_findings_snapshot_idx",
        table_name="filesystem_health_findings",
    )
    op.drop_table("filesystem_health_findings")
    op.drop_index(
        "filesystem_health_snapshots_vault_revision_idx",
        table_name="filesystem_health_snapshots",
    )
    op.drop_index(
        "filesystem_health_snapshots_vault_created_idx",
        table_name="filesystem_health_snapshots",
    )
    op.drop_table("filesystem_health_snapshots")
    op.drop_index("catalog_events_vault_revision_idx", table_name="catalog_events")
    op.drop_table("catalog_events")
    op.drop_table("vault_catalog_revisions")
