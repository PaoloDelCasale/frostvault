"""Upload Job rows on the rebuilt schema, shared by worker-level tests (#598).

``queue_upload`` writes what admission writes for one manual upload (the
narrow ``jobs`` row plus its ``job_uploads`` detail). ``claimed_job_row`` is
the row ``storage.process_job`` receives from the scheduler after a claim.
"""
from __future__ import annotations

from typing import Any


def queue_upload(
    connection: Any,
    file_id: str,
    *,
    vault_id: int = 2,
    total_bytes: int = 0,
    mode: str = "read_back",
    requested_at: Any = None,
    status: str = "queued",
    origin: str = "manual",
) -> int:
    job = connection.execute(
        """
        INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes,
                         requested_at, updated_at)
        VALUES (%s, %s, 'upload', %s, %s, %s, COALESCE(%s, now()), COALESCE(%s, now()))
        RETURNING id
        """,
        (vault_id, file_id, status, origin, total_bytes, requested_at, requested_at),
    ).fetchone()
    connection.execute(
        "INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, %s)",
        (job["id"], mode),
    )
    return int(job["id"])


JOB_ROW_SQL = """
    SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid, v.name AS vault_name,
           v.lifecycle AS vault_lifecycle, t.bucket AS s3_bucket
    FROM jobs j JOIN files f ON f.id=j.file_id JOIN vaults v ON v.id=j.vault_id
    JOIN vault_targets t ON t.id=v.target_id WHERE j.id=%s
"""


def job_row(connection: Any, job_id: int) -> dict[str, Any]:
    return connection.execute(JOB_ROW_SQL, (job_id,)).fetchone()


def claimed_job_row(connection: Any, job_id: int, claimed: dict[str, Any]) -> dict[str, Any]:
    """The worker's Job: its row joined as the scheduler reads it, with the claim."""
    return {**job_row(connection, job_id), "claim_token": claimed["claim_token"],
            "claim_expires_at": claimed["claim_expires_at"]}
