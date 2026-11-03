"""Remove offline cache authorization; retain the internal Session lock.

No Vault File, Archive Version, Local Copy or Job is changed. The old counter
becomes a private optimistic lock for concurrent Session transitions; the cache
nonce is removed. Existing Sessions keep their tokens and deadlines.

Revision ID: 0047_remove_offline_cache
Revises: 0046_simplify_notifications
"""
from __future__ import annotations

import secrets
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_remove_offline_cache"
down_revision: str | None = "0046_simplify_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_constraint("sessions_offline_cache_generation_ck", type_="check")
        batch.alter_column(
            "offline_cache_generation", new_column_name="transition_version",
            existing_type=sa.Integer(), existing_nullable=False, server_default="1",
        )
        batch.drop_column("offline_cache_nonce")
        batch.create_check_constraint("sessions_transition_version_ck", "transition_version >= 1")


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_constraint("sessions_transition_version_ck", type_="check")
        batch.alter_column(
            "transition_version", new_column_name="offline_cache_generation",
            existing_type=sa.Integer(), existing_nullable=False, server_default=None,
        )
        batch.add_column(sa.Column("offline_cache_nonce", sa.Text(), nullable=False, server_default=""))
        batch.create_check_constraint("sessions_offline_cache_generation_ck", "offline_cache_generation >= 1")
    connection = op.get_bind()
    for session_id in connection.execute(sa.text("SELECT id FROM sessions")).scalars().all():
        connection.execute(
            sa.text("UPDATE sessions SET offline_cache_nonce=:nonce WHERE id=:id"),
            {"nonce": secrets.token_urlsafe(32), "id": session_id},
        )
    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("offline_cache_nonce", server_default=None)
