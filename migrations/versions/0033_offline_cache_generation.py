"""Persist monotonic, unguessable offline-cache authorization generations.

Revision ID: 0033_offline_cache_generation
Revises: 0032_notification_inbox
"""
from __future__ import annotations

import secrets
from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0033_offline_cache_generation"
down_revision: str | None = "0032_notification_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GENERATION_INITIAL = 1


def _nonce() -> str:
    # Keep the migration's values compatible with sessions._new_offline_cache_nonce
    # without importing application configuration during Alembic execution.
    return secrets.token_urlsafe(32)


def upgrade() -> None:
    generation_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(
            sa.Column(
                "offline_cache_generation",
                generation_type,
                nullable=False,
                server_default=sa.text(str(_GENERATION_INITIAL)),
            )
        )
        batch.add_column(
            sa.Column(
                "offline_cache_nonce",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.create_check_constraint(
            "sessions_offline_cache_generation_ck",
            "offline_cache_generation >= 1",
        )

    # Existing live Sessions must not retain a deterministic pre-migration
    # authorization. Backfill an independent random nonce before dropping the
    # temporary empty-string default used to make the column non-nullable.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM sessions")).mappings()
    update = sa.text(
        "UPDATE sessions SET offline_cache_nonce=:nonce WHERE id=:session_id"
    )
    for row in rows:
        bind.execute(update, {"nonce": _nonce(), "session_id": row["id"]})

    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("offline_cache_generation", server_default=None)
        batch.alter_column("offline_cache_nonce", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_constraint("sessions_offline_cache_generation_ck", type_="check")
        batch.drop_column("offline_cache_nonce")
        batch.drop_column("offline_cache_generation")
