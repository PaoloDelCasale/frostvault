"""Append-only transactional directory invalidations (#458).

Metadata only. Old marks are retained. Writers never update a mark owned by
another transaction; maintenance consumes only identities in its snapshot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0061_aggregate_dirty_journal"
down_revision = "0060_vault_class_moves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("directory_aggregate_dirty", sa.Column(
        "mark_id", sa.Text(), nullable=False,
        # The server-start epoch prevents an imported journal from colliding
        # with xid values on a different/restored PostgreSQL cluster.
        server_default=sa.text("(pg_current_xact_id()::text || '@' || "
                               "extract(epoch from pg_postmaster_start_time())::text)"),
    ))
    op.drop_constraint("directory_aggregate_dirty_pkey", "directory_aggregate_dirty", type_="primary")
    op.create_primary_key("directory_aggregate_dirty_pkey", "directory_aggregate_dirty",
                          ["vault_id", "path", "mark_id"])


def downgrade() -> None:
    # Coalesce pending signals, never discard an invalidated path. The old
    # consumer only needs one mark per path; preserve its latest timestamp.
    op.execute("""UPDATE directory_aggregate_dirty d SET marked_at=s.marked_at
        FROM (SELECT vault_id, path, MAX(marked_at) AS marked_at
              FROM directory_aggregate_dirty GROUP BY vault_id, path) s
        WHERE d.vault_id=s.vault_id AND d.path=s.path""")
    op.execute("""DELETE FROM directory_aggregate_dirty a USING directory_aggregate_dirty b
        WHERE a.vault_id=b.vault_id AND a.path=b.path AND a.mark_id < b.mark_id""")
    op.drop_constraint("directory_aggregate_dirty_pkey", "directory_aggregate_dirty", type_="primary")
    op.drop_column("directory_aggregate_dirty", "mark_id")
    op.create_primary_key("directory_aggregate_dirty_pkey", "directory_aggregate_dirty", ["vault_id", "path"])
