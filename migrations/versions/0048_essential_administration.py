"""Remove runtime overrides and the worker diagnostic store (#416).

Configuration is now environment-only; operators must supply formerly
persisted overrides in the deployment before upgrading. No catalog, cloud,
Session, Job, notification or Source Volume data is changed.
"""
from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op

revision = "0048_essential_administration"
down_revision = "0047_remove_offline_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("system_settings")
    op.drop_table("worker_errors")


def downgrade() -> None:
    # Schema-only downgrade: removed overrides and diagnostics are not restored.
    importlib.import_module("migrations.versions.0024_system_settings").upgrade()
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "worker_errors",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="SET NULL")),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "classification IN ('timeout', 'connectivity', 'permission', 'configuration', 'unexpected')",
            name="worker_errors_classification_ck",
        ),
    )
    op.create_index("worker_errors_created_idx", "worker_errors", ["created_at"])
