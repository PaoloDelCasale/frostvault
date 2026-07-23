"""Add operator role, one-owner-per-vault invariant, and vault namespaces.

Revision ID: 0007_vault_ownership
Revises: 0006_auth_backoff
"""
from __future__ import annotations

from typing import Any, Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_vault_ownership"
down_revision: str | None = "0006_auth_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLite has no built-in UUID generator, so synthesize a UUIDv4-shaped value
# from random blobs. This only needs to be collision-resistant, not
# cryptographically significant: it is a storage namespace label, not a secret.
SQLITE_UUID_DEFAULT = sa.text(
    "(lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
    "substr(hex(randomblob(2)), 2) || '-' || "
    "substr('89ab', abs(random()) % 4 + 1, 1) || "
    "substr(hex(randomblob(2)), 2) || '-' || hex(randomblob(6))))"
)
POSTGRESQL_UUID_DEFAULT = sa.text("gen_random_uuid()")


def _reject_vaults_without_owner(connection: Any) -> None:
    """Fail before DDL when no existing membership is authorized to own.

    Promoting a legacy viewer would silently widen access. An operator must
    therefore choose and assign an owner explicitly before retrying.
    """
    owner_vault_ids = {
        row["vault_id"]
        for row in connection.execute(
            sa.text(
                "SELECT DISTINCT vault_id FROM vault_members WHERE role = 'owner'"
            )
        ).mappings().all()
    }
    ownerless = [
        row["id"]
        for row in connection.execute(
            sa.text("SELECT id FROM vaults ORDER BY id")
        ).mappings().all()
        if row["id"] not in owner_vault_ids
    ]
    if not ownerless:
        return
    ids = ", ".join(str(vault_id) for vault_id in ownerless)
    noun = "vault" if len(ownerless) == 1 else "vaults"
    verb = "has" if len(ownerless) == 1 else "have"
    raise RuntimeError(
        f"Cannot migrate vault ownership: {noun} {ids} {verb} no primary owner; "
        "assign exactly one authorized existing member the owner role before retrying"
    )


def _demote_extra_owners(connection: Any) -> None:
    """Keep the lowest-id existing owner and narrow every additional owner.

    Only legacy owners are candidates, so the deterministic choice never
    promotes a viewer or otherwise widens access. Membership rows are kept.
    """
    owners = connection.execute(
        sa.text(
            "SELECT vault_id, user_id FROM vault_members "
            "WHERE role = 'owner' ORDER BY vault_id, user_id"
        )
    ).mappings().all()
    seen_vaults: set[Any] = set()
    for owner in owners:
        vault_id = owner["vault_id"]
        if vault_id not in seen_vaults:
            seen_vaults.add(vault_id)
            continue
        connection.execute(
            sa.text(
                "UPDATE vault_members SET role = 'operator' "
                "WHERE vault_id = :vault_id AND user_id = :user_id"
            ),
            {"vault_id": vault_id, "user_id": owner["user_id"]},
        )


def upgrade() -> None:
    connection = op.get_bind()
    # Validate before any schema change so both SQLite and PostgreSQL leave
    # the database wholly at 0006 on an actionable ownerless-data failure.
    _reject_vaults_without_owner(connection)

    # Widen the role check constraint first so the demotion below (which
    # writes 'operator') is never blocked by the old, narrower constraint.
    with op.batch_alter_table("vault_members") as batch_op:
        batch_op.drop_constraint("vault_members_role_ck", type_="check")
        batch_op.create_check_constraint(
            "vault_members_role_ck",
            "role IN ('owner', 'operator', 'viewer')",
        )

    _demote_extra_owners(connection)

    one_owner = sa.text("role = 'owner'")
    op.create_index(
        "vault_members_one_owner_uq",
        "vault_members",
        ["vault_id"],
        unique=True,
        sqlite_where=one_owner,
        postgresql_where=one_owner,
    )

    uuid_default = (
        POSTGRESQL_UUID_DEFAULT
        if connection.dialect.name == "postgresql"
        else SQLITE_UUID_DEFAULT
    )
    with op.batch_alter_table("vaults") as batch_op:
        batch_op.add_column(
            sa.Column(
                "uuid",
                sa.String(36),
                nullable=False,
                server_default=uuid_default,
            )
        )
    op.create_index("vaults_uuid_uq", "vaults", ["uuid"], unique=True)


def downgrade() -> None:
    op.drop_index("vaults_uuid_uq", table_name="vaults")
    with op.batch_alter_table("vaults") as batch_op:
        batch_op.drop_column("uuid")

    op.drop_index("vault_members_one_owner_uq", table_name="vault_members")

    # The pre-0007 constraint has no 'operator' role, so narrow any operator
    # memberships to read-only rather than leave data the old check rejects.
    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE vault_members SET role = 'viewer' WHERE role = 'operator'")
    )

    with op.batch_alter_table("vault_members") as batch_op:
        batch_op.drop_constraint("vault_members_role_ck", type_="check")
        batch_op.create_check_constraint(
            "vault_members_role_ck",
            "role IN ('owner', 'viewer')",
        )
