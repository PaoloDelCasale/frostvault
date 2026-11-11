"""Durable runnable-but-unserved Vault turns (#552).

Revision ID: scheduler_turns_552
Revises: 0070_vault_pause
"""
from alembic import op

revision = "scheduler_turns_552"
down_revision = "0070_vault_pause"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE scheduler_vault_turns (
            vault_id BIGINT PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
            wait_seconds DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (wait_seconds >= 0),
            eligible_since TIMESTAMPTZ,
            last_served_at TIMESTAMPTZ,
            generation BIGINT NOT NULL DEFAULT 0
        )
    """)
    # Invalidate the open observation interval, NOT banked credit. Changes can
    # happen entirely between scheduler polls (or while the process is down).
    # Discarding that partial interval is deliberately conservative: blocked
    # time must never become starvation credit. No trigger on progress writes,
    # heartbeat renewal, admission INSERTs, or claim acquisition.
    op.execute("""
        CREATE FUNCTION scheduler_invalidate_turn_552() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE scheduler_vault_turns SET eligible_since=NULL
            WHERE vault_id=OLD.vault_id AND eligible_since IS NOT NULL;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER scheduler_job_state_552
        AFTER UPDATE OF status, retry_after, pending_until ON jobs
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status
                        OR OLD.retry_after IS DISTINCT FROM NEW.retry_after
                        OR OLD.pending_until IS DISTINCT FROM NEW.pending_until)
        EXECUTE FUNCTION scheduler_invalidate_turn_552()
    """)
    op.execute("""
        CREATE TRIGGER scheduler_job_deleted_552 AFTER DELETE ON jobs
        FOR EACH ROW EXECUTE FUNCTION scheduler_invalidate_turn_552()
    """)
    op.execute("""
        CREATE TRIGGER scheduler_upload_policy_552
        AFTER UPDATE OF auto_upload ON vault_operation_policies
        FOR EACH ROW WHEN (OLD.auto_upload IS DISTINCT FROM NEW.auto_upload)
        EXECUTE FUNCTION scheduler_invalidate_turn_552()
    """)
    op.execute("""
        CREATE FUNCTION scheduler_invalidate_vault_turn_552() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE scheduler_vault_turns SET eligible_since=NULL
            WHERE vault_id=OLD.id AND eligible_since IS NOT NULL;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER scheduler_vault_state_552
        AFTER UPDATE OF paused, enabled, relocation_state, decommission_state ON vaults
        FOR EACH ROW WHEN (OLD.paused IS DISTINCT FROM NEW.paused
                        OR OLD.enabled IS DISTINCT FROM NEW.enabled
                        OR OLD.relocation_state IS DISTINCT FROM NEW.relocation_state
                        OR OLD.decommission_state IS DISTINCT FROM NEW.decommission_state)
        EXECUTE FUNCTION scheduler_invalidate_vault_turn_552()
    """)


def downgrade():
    op.execute("DROP TRIGGER scheduler_vault_state_552 ON vaults")
    op.execute("DROP FUNCTION scheduler_invalidate_vault_turn_552()")
    op.execute("DROP TRIGGER scheduler_upload_policy_552 ON vault_operation_policies")
    op.execute("DROP TRIGGER scheduler_job_deleted_552 ON jobs")
    op.execute("DROP TRIGGER scheduler_job_state_552 ON jobs")
    op.execute("DROP FUNCTION scheduler_invalidate_turn_552()")
    op.execute("DROP TABLE scheduler_vault_turns")
