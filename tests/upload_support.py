"""Upload Job rows on the rebuilt schema, shared by worker-level tests (#598).

``queue_upload`` writes what admission writes for one manual upload (the
narrow ``jobs`` row plus its ``job_uploads`` detail). ``claimed_job_row`` is
the row ``storage.process_job`` receives from the scheduler after a claim.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


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
           v.lifecycle AS vault_lifecycle, t.bucket AS s3_bucket, t.key_layout AS s3_key_layout
    FROM jobs j JOIN files f ON f.id=j.file_id JOIN vaults v ON v.id=j.vault_id
    JOIN vault_targets t ON t.id=v.target_id WHERE j.id=%s
"""


def job_row(connection: Any, job_id: int) -> dict[str, Any]:
    return connection.execute(JOB_ROW_SQL, (job_id,)).fetchone()


def claimed_job_row(connection: Any, job_id: int, claimed: dict[str, Any]) -> dict[str, Any]:
    """The worker's Job: its row joined as the scheduler reads it, with the claim."""
    return {**job_row(connection, job_id), "claim_token": claimed["claim_token"],
            "claim_expires_at": claimed["claim_expires_at"]}


def fake_worker() -> MagicMock:
    """The fake Adapter of the worker seam, for tests of the Upload/copy Modules alone.

    It has the names of the production Adapter and no other (``spec_set``): a
    Module that asks the worker for anything outside the seam fails here. The
    exceptions and the automatic-gate states are the real ones; every method is
    a mock a test gives its own return value or side effect. By default the
    worker is idle: no stop signal and no lost claim.
    """
    from app import storage
    from app.services import worker_seam

    worker = MagicMock(spec_set=storage.worker_adapter)
    for seam in (worker_seam.JobClaim, worker_seam.SourceVolume, worker_seam.AutomaticUploadGate):
        for name in seam.__annotations__:
            setattr(worker, name, getattr(storage.worker_adapter, name))
    worker.claim_lost_event.return_value = None
    worker.stopped_in_process.return_value = False
    worker.job_cancelled.return_value = False
    return worker
