"""Temporary Owner-domain checkpoint for ADR-0035 / #578.

This revision is assembled into the new baseline by the parent branch. It is
not an authorization to upgrade an old deployment: entrypoint schema guards
remain responsible for rejecting incompatible catalogs before mutation.
"""
from alembic import op
import sqlalchemy as sa

revision = "0074_owner_singleton"
down_revision = "0073_obligation_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # This checkpoint must not silently choose between multiple principals or
    # discard an identity. The final baseline starts empty.
    if connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one() > 1:
        raise RuntimeError("Owner singleton cannot adopt multiple principals")
    if connection.execute(sa.text("SELECT count(*) FROM user_identities")).scalar_one() > 1:
        raise RuntimeError("Owner singleton cannot adopt multiple identity bindings")
    op.create_table(
        "owner",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("issuer", sa.Text()),
        sa.Column("subject", sa.Text()),
        sa.CheckConstraint("id = 1", name="owner_singleton_ck"),
        sa.CheckConstraint("(issuer IS NULL) = (subject IS NULL)", name="owner_identity_pair_ck"),
    )
    op.execute("""INSERT INTO owner(id,display_name,password_hash,session_version,issuer,subject)
        SELECT 1,u.display_name,u.password_hash,u.session_version,i.issuer,i.subject
        FROM users u LEFT JOIN user_identities i ON i.user_id=u.id""")
    op.add_column("audit_events", sa.Column("actor", sa.Text(), nullable=False, server_default="system"))
    op.execute("""UPDATE audit_events SET actor=CASE
        WHEN event LIKE 'break_glass_%' OR event IN ('reauth_failed','reauth_succeeded')
          OR (event='auth_backoff_blocked' AND detail_json::jsonb->>'flow' IN ('break_glass','reauth'))
          THEN 'emergency'
        WHEN event='local_cleanup.auto_queued' OR EXISTS (
            SELECT 1 FROM jobs j WHERE j.id=audit_events.job_id AND j.origin='automatic'
        ) THEN 'system'
        WHEN actor_user_id IS NOT NULL THEN 'owner' ELSE 'system' END""")
    op.create_check_constraint("audit_events_actor_ck", "audit_events", "actor IN ('owner','emergency','system')")
    for table, columns in {
        "sessions": ("user_id",),
        "notifications": ("user_id",),
        "jobs": ("requested_by", "approved_by"),
        "bulk_upload_admissions": ("requested_by",),
        "protection_stops": ("requested_by",),
        "audit_events": ("actor_user_id",),
    }.items():
        # PostgreSQL drops dependent indexes/constraints with the column; no
        # CASCADE is used, so an unexpected external dependency fails closed.
        for column in columns:
            op.drop_column(table, column)
    op.create_index("notifications_dedupe_key_uq", "notifications", ["dedupe_key"], unique=True)
    op.create_index("notifications_created_idx", "notifications", ["created_at"])
    op.create_index("notifications_open_problems_idx", "notifications", ["id"],
                    postgresql_where=sa.text("problem_status='open'"))
    op.drop_table("user_identities")
    op.drop_table("users")


def downgrade() -> None:
    # Historical empty-schema roundtrip coverage remains possible. Never
    # manufacture old usernames/authorship for a populated singleton catalog.
    connection = op.get_bind()
    for table in ("owner", "sessions", "notifications", "jobs", "audit_events",
                  "bulk_upload_admissions", "protection_stops"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Cannot downgrade a populated Owner singleton catalog")
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("users_username_lower_uq", "users", [sa.text("lower(username)")], unique=True)
    op.create_table(
        "user_identities",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="user_identities_issuer_subject_uq"),
    )
    op.create_index("user_identities_user_idx", "user_identities", ["user_id"])
    op.create_index("user_identities_issuer_uq", "user_identities", ["issuer"], unique=True)
    for table, column, nullable, ondelete in (
        ("sessions", "user_id", False, "CASCADE"),
        ("notifications", "user_id", False, "CASCADE"),
        ("jobs", "requested_by", True, "SET NULL"),
        ("protection_stops", "requested_by", True, "SET NULL"),
        ("audit_events", "actor_user_id", True, "SET NULL"),
    ):
        op.add_column(table, sa.Column(column, sa.BigInteger(), nullable=nullable))
        op.create_foreign_key(f"{table}_{column}_fkey", table, "users", [column], ["id"], ondelete=ondelete)
    op.add_column("jobs", sa.Column("approved_by", sa.Integer()))
    op.add_column("bulk_upload_admissions", sa.Column("requested_by", sa.BigInteger(), nullable=False))
    op.create_index("sessions_user_idx", "sessions", ["user_id"])
    op.drop_index("notifications_dedupe_key_uq", table_name="notifications")
    op.drop_index("notifications_created_idx", table_name="notifications")
    op.drop_index("notifications_open_problems_idx", table_name="notifications")
    op.create_index("notifications_user_dedupe_uq", "notifications", ["user_id", "dedupe_key"], unique=True,
                    postgresql_where=sa.text("dedupe_key IS NOT NULL"))
    op.create_index("notifications_user_created_idx", "notifications", ["user_id", "created_at"])
    op.create_index("notifications_open_problems_idx", "notifications", ["user_id"],
                    postgresql_where=sa.text("problem_status='open'"))
    op.drop_column("audit_events", "actor")
    op.drop_table("owner")
