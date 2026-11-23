"""The removal of every catalog row of one Vault (ADR-0047).

Two callers, each in the transaction that also removes the Vault row, with
that row already locked: a Vault Deletion whose prefix a fresh listing proved
empty (``vault_deletion``), and the cancellation of a Vault creation
(``vault_buckets.cancel_creation``). The caller decides whether the Vault may
go; this module only knows which rows are its catalog and in which order the
foreign keys let them leave. It names no S3 object and no local file.
"""
from __future__ import annotations

from typing import Any


def remove_vault_catalog(connection: Any, vault_id: int) -> dict[str, int]:
    """Every catalog row of the Vault, in foreign-key order; audit rows stay."""
    counts = connection.execute(
        """SELECT (SELECT count(*) FROM files WHERE vault_id=%(v)s) AS files,
                  (SELECT count(*) FROM archive_versions av JOIN files f ON f.id=av.file_id
                    WHERE f.vault_id=%(v)s) AS archive_versions,
                  (SELECT count(*) FROM jobs WHERE vault_id=%(v)s) AS jobs""",
        {"v": vault_id}).fetchone()
    files = "SELECT id FROM files WHERE vault_id=%(v)s"
    jobs = "SELECT id FROM jobs WHERE vault_id=%(v)s"
    statements = (
        f"DELETE FROM notifications WHERE vault_id=%(v)s OR job_id IN ({jobs})",
        f"DELETE FROM cleanup_candidates WHERE file_id IN ({files})",
        "DELETE FROM cleanup_batches WHERE vault_id=%(v)s",
        "DELETE FROM upload_remediations WHERE vault_id=%(v)s",
        "DELETE FROM jobs WHERE vault_id=%(v)s",
        "DELETE FROM job_groups WHERE vault_id=%(v)s",
        """UPDATE files SET current_version_id=NULL, replacing_version_id=NULL,
               matched_version_id=NULL, held_version_id=NULL
           WHERE vault_id=%(v)s AND (current_version_id IS NOT NULL
               OR replacing_version_id IS NOT NULL OR matched_version_id IS NOT NULL
               OR held_version_id IS NOT NULL)""",
        f"DELETE FROM archive_versions WHERE file_id IN ({files})",
        f"DELETE FROM upload_attempts WHERE file_id IN ({files})",
        "DELETE FROM upload_snapshots WHERE vault_id=%(v)s",
        "DELETE FROM copy_attempts WHERE vault_id=%(v)s",
        "DELETE FROM files WHERE vault_id=%(v)s",
    )
    for statement in statements:
        connection.execute(statement, {"v": vault_id})
    return {key: int(counts[key] or 0) for key in ("files", "archive_versions", "jobs")}
