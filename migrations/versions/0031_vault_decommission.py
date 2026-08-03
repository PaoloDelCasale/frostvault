"""Add the auditable Vault decommission and root-release lifecycle.

Revision ID: 0031_vault_decommission
Revises: 0030_vault_root_relocation
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_vault_decommission"
down_revision: str | None = "0030_vault_root_relocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(
            sa.Column(
                "decommission_state",
                sa.Text(),
                nullable=False,
                server_default="active",
            )
        )
        batch.add_column(sa.Column("decommissioned_at", sa.Text(), nullable=True))
        batch.add_column(sa.Column("root_released_at", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "vaults_decommission_state_ck",
            "decommission_state IN ('active', 'decommissioning', 'decommissioned')",
        )
        batch.create_check_constraint(
            "vaults_decommission_terminal_ck",
            "(decommission_state = 'decommissioned' "
            "AND decommissioned_at IS NOT NULL AND root_released_at IS NOT NULL) "
            "OR (decommission_state <> 'decommissioned' "
            "AND decommissioned_at IS NULL AND root_released_at IS NULL)",
        )
    op.create_index(
        "vaults_root_occupancy_idx",
        "vaults",
        ["root_released_at", "source_root"],
    )

    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_origin_ck", type_="check")
        batch.create_check_constraint(
            "jobs_origin_ck",
            "origin IN ('manual', 'automatic', 'decommission')",
        )

    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "vault_decommissions",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column(
            "vault_id",
            identifier,
            sa.ForeignKey("vaults.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "requested_by",
            identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("local_disposition", sa.Text(), nullable=False),
        sa.Column("cloud_disposition", sa.Text(), nullable=False),
        sa.Column("local_status", sa.Text(), nullable=False),
        sa.Column("cloud_status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("preview_json", sa.Text(), nullable=False),
        sa.Column("local_job_group_id", sa.String(length=36), nullable=True),
        sa.Column("cloud_job_group_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('quiescing', 'local_cleanup', 'cloud_purge', "
            "'finalizing', 'blocked', 'completed')",
            name="vault_decommissions_state_ck",
        ),
        sa.CheckConstraint(
            "local_disposition IN ('retain', 'remove')",
            name="vault_decommissions_local_disposition_ck",
        ),
        sa.CheckConstraint(
            "cloud_disposition IN ('retain', 'purge')",
            name="vault_decommissions_cloud_disposition_ck",
        ),
        sa.CheckConstraint(
            "local_status IN ('pending', 'retained', 'removing', 'removed')",
            name="vault_decommissions_local_status_ck",
        ),
        sa.CheckConstraint(
            "cloud_status IN ('pending', 'retained', 'purging', 'purged')",
            name="vault_decommissions_cloud_status_ck",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 3 AND 500",
            name="vault_decommissions_reason_ck",
        ),
    )
    op.create_index(
        "vault_decommissions_state_idx",
        "vault_decommissions",
        ["state", "updated_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(
        sa.text("SELECT COUNT(*) FROM vault_decommissions")
    ).scalar_one()
    released = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM vaults "
            "WHERE decommission_state <> 'active' OR root_released_at IS NOT NULL"
        )
    ).scalar_one()
    if int(used or 0) or int(released or 0):
        raise RuntimeError(
            "Cannot downgrade after a Vault decommission has started; "
            "the root-release tombstone would be lost"
        )

    op.drop_index("vault_decommissions_state_idx", table_name="vault_decommissions")
    op.drop_table("vault_decommissions")

    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_origin_ck", type_="check")
        batch.create_check_constraint(
            "jobs_origin_ck",
            "origin IN ('manual', 'automatic')",
        )

    op.drop_index("vaults_root_occupancy_idx", table_name="vaults")
    with op.batch_alter_table("vaults") as batch:
        batch.drop_constraint("vaults_decommission_terminal_ck", type_="check")
        batch.drop_constraint("vaults_decommission_state_ck", type_="check")
        batch.drop_column("root_released_at")
        batch.drop_column("decommissioned_at")
        batch.drop_column("decommission_state")
