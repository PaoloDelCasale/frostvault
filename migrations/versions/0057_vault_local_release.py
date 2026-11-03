"""One opt-in local release period per Vault (#426).

The legacy global environment gate cannot be determined from database state;
old filters also limited the scope. Every old policy therefore maps to OFF,
including formerly enabled policies. No filesystem/cloud calls or trust changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_vault_local_release"
down_revision = "0056_notification_problems"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A crashed rename-claim must be reconciled by the previous release before
    # changing its Job state. Never strand a user's bytes in a hidden claim.
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM jobs WHERE action='free-space' AND status='cleaning' LIMIT 1"
    )).first():
        raise RuntimeError("Reconcile interrupted free-space Jobs with the previous release before upgrading")
    op.add_column("vault_operation_policies", sa.Column("local_release_after_days", sa.Integer(), nullable=True))
    op.create_check_constraint("vault_operation_policies_release_period_ck", "vault_operation_policies",
                               "local_release_after_days IN (90, 180, 365, 730)")
    op.drop_constraint("vault_operation_policies_retention_positive_ck", "vault_operation_policies", type_="check")
    op.drop_column("vault_operation_policies", "local_retention_days")
    op.drop_column("vault_operation_policies", "auto_local_cleanup")
    # Old queued/restartable work must not gain deletion authority by removing
    # the global gate. Cancel even explicit legacy work: request it again safely.
    op.execute("""
        UPDATE jobs SET status='cancelled', claim_token=NULL, claimed_at=NULL,
            claim_expires_at=NULL, updated_at=CURRENT_TIMESTAMP::text,
            message='Local release settings changed; request release again explicitly',
            message_key=NULL, message_params=NULL
        WHERE action='free-space' AND status NOT IN ('completed','failed','cancelled')
    """)


def downgrade() -> None:
    # Downgrade is also non-destructive: never reactivate old cleanup.
    op.add_column("vault_operation_policies", sa.Column("auto_local_cleanup", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("vault_operation_policies", sa.Column("local_retention_days", sa.Integer(), nullable=True))
    op.create_check_constraint("vault_operation_policies_retention_positive_ck", "vault_operation_policies",
                               "local_retention_days IS NULL OR local_retention_days > 0")
    op.drop_constraint("vault_operation_policies_release_period_ck", "vault_operation_policies", type_="check")
    op.drop_column("vault_operation_policies", "local_release_after_days")
