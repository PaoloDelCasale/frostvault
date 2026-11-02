"""Single owner: drop Users administration, Invites, roles, and Source Areas.

FrostVault has exactly one User, the owner (#412, #366 Q1/Q2). This removes
the multi-user schema: Vault memberships and roles, the global administrator
flag and deactivation, Invites, UI-managed OIDC configuration, the user
lookup rate limit, and Source Area grants. The owner keeps at most one OIDC
Identity per issuer, bound at the first login.

Existing data has no compatibility obligation (#362 D6): the lowest-id
administrator (or, failing that, the lowest-id User) becomes the owner and
every other User is removed.

Revision ID: 0044_single_owner
Revises: 0043_cloud_only_conflict
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0044_single_owner"
down_revision: str | None = "0043_cloud_only_conflict"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identifier() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _restore_username_index() -> None:
    # SQLite batch recreate cannot reflect the expression-based unique index.
    if op.get_bind().dialect.name == "sqlite":
        op.create_index(
            "users_username_lower_uq",
            "users",
            [sa.text("lower(username)")],
            unique=True,
        )


def _keep_only_owner(connection: sa.engine.Connection) -> None:
    owner = connection.execute(
        sa.text(
            "SELECT id FROM users ORDER BY CASE WHEN is_admin THEN 0 ELSE 1 END, id "
            "LIMIT 1"
        )
    ).scalar()
    if owner is None:
        return
    # system_settings.updated_by is RESTRICT: attribute history to the owner.
    connection.execute(
        sa.text("UPDATE system_settings SET updated_by=:owner WHERE updated_by<>:owner"),
        {"owner": owner},
    )
    # SQLite migrations run without foreign-key enforcement, so remove the
    # authentication rows of other Users explicitly instead of by cascade.
    for table in ("user_identities", "sessions"):
        connection.execute(
            sa.text(f"DELETE FROM {table} WHERE user_id<>:owner"), {"owner": owner}
        )
    connection.execute(sa.text("DELETE FROM users WHERE id<>:owner"), {"owner": owner})
    # One Identity per issuer: keep the oldest binding.
    connection.execute(
        sa.text(
            """
            DELETE FROM user_identities
            WHERE id NOT IN (
                SELECT MIN(id) FROM user_identities GROUP BY issuer
            )
            """
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("oidc_login") as batch:
        batch.drop_constraint("oidc_login_invite_fk", type_="foreignkey")
        batch.drop_column("invite_id")
    op.drop_index("invites_pending_idx", table_name="invites")
    op.drop_index("invites_target_user_idx", table_name="invites")
    op.drop_table("invites")
    op.drop_table("oidc_configuration")
    op.drop_index(
        "lookup_rate_limit_attempts_key_time_idx",
        table_name="lookup_rate_limit_attempts",
    )
    op.drop_table("lookup_rate_limit_attempts")
    op.drop_table("lookup_rate_limit_keys")
    op.drop_index("source_areas_volume_idx", table_name="source_areas")
    op.drop_index("source_areas_user_idx", table_name="source_areas")
    op.drop_table("source_areas")
    op.drop_index("vault_members_one_owner_uq", table_name="vault_members")
    op.drop_table("vault_members")

    # The Invite lifetime is no longer a runtime-managed setting.
    op.execute("DELETE FROM system_settings WHERE key='invite_ttl_seconds'")
    _keep_only_owner(op.get_bind())

    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_admin")
        batch.drop_column("active")
    _restore_username_index()
    op.create_index(
        "user_identities_issuer_uq", "user_identities", ["issuer"], unique=True
    )


def downgrade() -> None:
    identifier = _identifier()
    op.drop_index("user_identities_issuer_uq", table_name="user_identities")
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    _restore_username_index()
    op.execute("UPDATE users SET is_admin=TRUE")

    op.create_table(
        "vault_members",
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.Text(), nullable=False, server_default="owner"),
        sa.CheckConstraint(
            "role IN ('owner', 'operator', 'viewer')", name="vault_members_role_ck"
        ),
    )
    one_owner = sa.text("role = 'owner'")
    op.create_index(
        "vault_members_one_owner_uq",
        "vault_members",
        ["vault_id"],
        unique=True,
        sqlite_where=one_owner,
        postgresql_where=one_owner,
    )
    op.execute(
        """
        INSERT INTO vault_members(vault_id, user_id, role)
        SELECT v.id, (SELECT MIN(id) FROM users), 'owner'
        FROM vaults v
        WHERE EXISTS (SELECT 1 FROM users)
        """
    )

    op.create_table(
        "source_areas",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("volume_alias", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "volume_alias", "relative_path", name="source_areas_volume_relative_uq"
        ),
    )
    op.create_index("source_areas_user_idx", "source_areas", ["user_id"])
    op.create_index("source_areas_volume_idx", "source_areas", ["volume_alias"])

    op.create_table(
        "lookup_rate_limit_keys",
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("client_ip", sa.Text(), primary_key=True),
    )
    op.create_table(
        "lookup_rate_limit_attempts",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_ip", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "lookup_rate_limit_attempts_key_time_idx",
        "lookup_rate_limit_attempts",
        ["user_id", "client_ip", "attempted_at"],
    )

    op.create_table(
        "oidc_configuration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_enabled", sa.Boolean(), nullable=False),
        sa.Column("active_version", sa.Integer()),
        sa.Column("active_issuer", sa.Text()),
        sa.Column("active_client_id", sa.Text()),
        sa.Column("active_secret_ciphertext", sa.Text()),
        sa.Column("active_scopes", sa.JSON()),
        sa.Column("active_login_ttl_seconds", sa.Integer()),
        sa.Column("draft_version", sa.Integer()),
        sa.Column("draft_issuer", sa.Text()),
        sa.Column("draft_client_id", sa.Text()),
        sa.Column("draft_secret_ciphertext", sa.Text()),
        sa.Column("draft_scopes", sa.JSON()),
        sa.Column("draft_login_ttl_seconds", sa.Integer()),
        sa.Column("validated_draft_version", sa.Integer()),
        sa.Column("validation_status", sa.Text()),
        sa.Column("validation_error", sa.Text()),
        sa.Column("validated_at", sa.Text()),
        sa.Column(
            "updated_by", identifier, sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="oidc_configuration_singleton_ck"),
        sa.CheckConstraint(
            "validation_status IS NULL OR validation_status IN "
            "('not_validated', 'valid', 'invalid')",
            name="oidc_configuration_validation_status_ck",
        ),
    )

    op.create_table(
        "invites",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "target_user_id", identifier,
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_by", identifier, sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("redeemed_at", sa.Text()),
        sa.Column("redeemed_issuer", sa.Text()),
        sa.Column("redeemed_subject", sa.Text()),
        sa.Column("revoked_at", sa.Text()),
        sa.Column(
            "revoked_by", identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL", name="invites_revoked_by_fk"),
        ),
    )
    op.create_index("invites_target_user_idx", "invites", ["target_user_id"])
    pending = sa.text("redeemed_at IS NULL AND revoked_at IS NULL")
    op.create_index(
        "invites_pending_idx",
        "invites",
        ["expires_at"],
        sqlite_where=pending,
        postgresql_where=pending,
    )
    with op.batch_alter_table("oidc_login") as batch:
        batch.add_column(sa.Column("invite_id", identifier))
        batch.create_foreign_key(
            "oidc_login_invite_fk", "invites", ["invite_id"], ["id"],
            ondelete="SET NULL",
        )
