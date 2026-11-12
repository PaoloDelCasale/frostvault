"""Discover durable upload obligations without a redundant per-upload queue.

Temporary incremental checkpoint for ADR-0035, to be folded into its baseline.
No evidence, cancellation, integrity or cleanup authority is changed by this DDL.
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_obligation_discovery"
down_revision = "0072_upload_lineage_indexes"
branch_labels = None
depends_on = None

INDEXES = (
    ("upload_intents_open_send_idx", "upload_intents", ["vault_id", "created_at", "id"], "send_started AND NOT settled"),
    ("archive_versions_unverified_idx", "archive_versions", ["vault_id", "discovered_at", "id"], "integrity='unverified' AND availability='available'"),
    ("archive_versions_unverified_file_idx", "archive_versions", ["vault_file_id", "discovered_at", "id"], "integrity='unverified' AND availability='available'"),
    ("upload_remediations_intent_history_idx", "upload_remediations", ["intent_id"], "intent_id IS NOT NULL"),
    ("upload_remediations_version_history_idx", "upload_remediations", ["archive_version_id", "resolved_at"], "archive_version_id IS NOT NULL"),
)


def upgrade():
    for name, table, columns, predicate in INDEXES:
        op.create_index(name, table, columns, postgresql_where=sa.text(predicate))
    # The source rows survive independently of capture/drain or a crash. Ordinary
    # settled, verified uploads cease to create any discovery backlog.
    op.drop_table("upload_remediation_facts")
    op.execute("UPDATE upload_remediation_sweep SET name='obligations' WHERE name='facts'")


def downgrade():
    # Reconstitute the old metadata inbox from durable sources, not from invented
    # tokens/proofs or provider I/O. Do not lose cancelled ambiguous sends either.
    op.create_table("upload_remediation_facts",
        sa.Column("target_key", sa.Text(), primary_key=True),
        sa.Column("vault_id", sa.BigInteger(), nullable=False),
        sa.Column("vault_file_id", sa.Text(), nullable=False),
        sa.Column("intent_id", sa.Text()),
        sa.Column("archive_version_id", sa.Text()),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.Column("due_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["vault_file_id", "vault_id"], ["vault_files.id", "vault_files.vault_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["intent_id", "vault_file_id", "vault_id"], ["upload_intents.id", "upload_intents.vault_file_id", "upload_intents.vault_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["archive_version_id", "vault_file_id", "vault_id"], ["archive_versions.id", "archive_versions.vault_file_id", "archive_versions.vault_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("intent_id IS NOT NULL OR archive_version_id IS NOT NULL", name="upload_remediation_fact_target"))
    op.create_index("upload_remediation_facts_due_idx", "upload_remediation_facts", ["vault_id", "due_at", "target_key"])
    op.execute("""INSERT INTO upload_remediation_facts
        SELECT 'intent:'||i.id,i.vault_id,i.vault_file_id,i.id,i.archive_version_id,i.created_at,i.created_at
        FROM upload_intents i WHERE i.send_started AND NOT i.settled""")
    op.execute("""INSERT INTO upload_remediation_facts
        SELECT 'version:'||av.id,av.vault_id,av.vault_file_id,NULL,av.id,av.discovered_at,av.discovered_at
        FROM archive_versions av WHERE av.integrity='unverified' OR
            (av.integrity='verified' AND av.availability='available' AND EXISTS (
                SELECT 1 FROM snapshot_reconciliation_blocks b WHERE b.vault_file_id=av.vault_file_id))""")
    op.execute("UPDATE upload_remediation_sweep SET name='facts' WHERE name='obligations'")
    for name, table, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
