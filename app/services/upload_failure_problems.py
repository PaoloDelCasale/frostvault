"""DB-only closure proofs for failed-upload problems, never Job history edits.

Every expression uses the alias ``original`` for the historical failed Job.
Materialized cloud writes still require the exact remediation/cleanup proof.
Only a failure with no cloud work left to reconcile can be superseded by later
successful protection of the same stable Vault File's current Local Copy.
"""
from .protection_states import CURRENT_JOIN_SQL, PROTECTED_SQL
from .upload_remediation import CLOSED_SQL as REMEDIATION_CLOSED_SQL


# A missing file leaves nothing to protect only when no cloud fact was created.
REMOVED_FILE_FAILURE_SQL = """(
    original.action='upload' AND original.status='failed'
    AND original.archive_version_id IS NULL
    AND EXISTS (SELECT 1 FROM local_copies lc
                WHERE lc.vault_file_id=original.vault_file_id AND lc.presence='missing')
    AND NOT EXISTS (SELECT 1 FROM upload_intent_jobs ib
                    JOIN upload_intents i ON i.id=ib.intent_id
                    WHERE ib.job_id=original.id AND i.send_started)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs rj WHERE rj.job_id=original.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_originals ro WHERE ro.original_job_id=original.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_discovery q WHERE q.job_id=original.id)
)"""

SUPERSEDED_FAILURE_SQL = f"""(
    original.action='upload' AND original.status='failed'
    AND NOT original.upload_reconcile_only AND original.archive_version_id IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM upload_intents i
        WHERE (i.original_job_id=original.id OR EXISTS (
            SELECT 1 FROM upload_intent_jobs ib WHERE ib.intent_id=i.id AND ib.job_id=original.id
        )) AND (i.send_started OR i.archive_version_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM upload_receipts receipt WHERE receipt.intent_id=i.id
        ))
    )
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs rj WHERE rj.job_id=original.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_originals ro WHERE ro.original_job_id=original.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_discovery q WHERE q.job_id=original.id)
    AND EXISTS (
        SELECT 1 FROM files f
        {CURRENT_JOIN_SQL}
        WHERE f.id=original.file_id AND f.vault_id=original.vault_id
          AND f.held_version_id IS NULL
          AND {PROTECTED_SQL}
          AND EXISTS (
              SELECT 1 FROM jobs successful
              WHERE successful.file_id=f.id AND successful.vault_id=f.vault_id
                AND successful.action='upload' AND successful.status='completed'
                AND successful.id>original.id AND successful.updated_at>=original.updated_at
                AND successful.archive_version_id=cur.id
          )
    )
)"""

# Resolution is an episode outcome, not a live protection badge. Later edits
# must not resurrect an old failed Job in the backlog after its problem closed.
# Folded failures belong to the group's frozen interval, excluding own notices.
NOTIFIED_CLOSED_SQL = """EXISTS (
    SELECT 1 FROM notifications closed
    WHERE closed.problem_status='resolved' AND (
        (closed.event='job_failed' AND closed.job_id=original.id)
        OR (closed.event='job_failed_group' AND closed.vault_id=original.vault_id
            AND original.action='upload'
            AND original.updated_at >= (closed.message_params::jsonb->>'since')
            AND original.updated_at <= COALESCE(closed.message_params::jsonb->>'last','9999-12-31')
            AND NOT EXISTS (SELECT 1 FROM notifications own
                            WHERE own.job_id=original.id AND own.event='job_failed'))
    )
)"""

CLOSED_UPLOAD_FAILURE_SQL = f"({REMEDIATION_CLOSED_SQL} OR {REMOVED_FILE_FAILURE_SQL} OR {SUPERSEDED_FAILURE_SQL} OR {NOTIFIED_CLOSED_SQL})"
