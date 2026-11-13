"""Conservative metadata-only repair of historical managed-download residues.

Ordinary *.tmp.* user names are never reserved. Legacy, non-reserved names need
an exact completed Recover history, size and creation-window correlation. No
filesystem content, cloud Versions or Job history is removed: only a Vault File
row that never had any of them (no Archive Version, no Job, no closed path) is
dropped, the same provisional identity a confirmed rename consumes (#593).
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .directory_aggregates import mark_path_dirty
from .vault_paths import RESTORE_TEMPORARY_RE, VERIFY_TEMPORARY_RE, safe_local_entry_path

_LEGACY = re.compile(r"(.+)\.tmp\.[A-Za-z0-9]{8}")
_CANDIDATE = r"(^|/).+\.tmp(\.[A-Za-z0-9]{8}|\..+\.partial)?$"
_BATCH = 100
# A Vault File with nothing to keep: never protected, worked on or renamed.
_NO_HISTORY = """
    f.current_version_id IS NULL AND f.replacing_version_id IS NULL
    AND f.cloud_since IS NULL
    AND NOT EXISTS (SELECT 1 FROM archive_versions av WHERE av.file_id=f.id)
    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=f.id)
    AND NOT EXISTS (SELECT 1 FROM file_path_history h WHERE h.file_id=f.id)
"""


def retire_absent_restore_ghosts(connection: Any, vault_id: int, root: Path, at: str) -> int:
    """Idempotent, bounded, under the caller's normal local scan/mutation gate."""
    after = ""
    retired = 0
    while True:
        rows = connection.execute(f"""
            SELECT f.id, f.path, f.created_at, f.local_size AS size
            FROM files f
            WHERE f.vault_id=%s AND f.path>%s AND f.path ~ %s AND {_NO_HISTORY}
            ORDER BY f.path LIMIT %s
        """, (vault_id, after, _CANDIDATE, _BATCH)).fetchall()
        if not rows:
            return retired
        for row in rows:
            after = row["path"]
            name = Path(row["path"]).name
            owned = RESTORE_TEMPORARY_RE.fullmatch(name) or VERIFY_TEMPORARY_RE.fullmatch(name)
            if not owned:
                match = _LEGACY.fullmatch(row["path"])
                if match is None:
                    continue
                correlated = connection.execute("""
                    SELECT 1 FROM jobs j
                    JOIN files recovered ON recovered.id=j.file_id
                    JOIN archive_versions av ON av.id=j.archive_version_id AND av.file_id=j.file_id
                    WHERE j.vault_id=%s AND recovered.path=%s AND j.action='recover' AND j.status='completed'
                      AND j.requested_at<=%s AND j.updated_at>=%s AND av.size=%s
                    LIMIT 1
                """, (vault_id, match[1], row["created_at"], row["created_at"], row["size"])).fetchone()
                if not correlated:
                    continue
            try:
                candidate = safe_local_entry_path(str(root), row["path"])
                candidate.lstat()
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                continue
            else:
                continue  # Never hide a name that still exists on disk.
            # The guard is re-checked by the delete itself: a Job, version or
            # rename that landed since the read keeps the identity.
            dropped = connection.execute(
                f"DELETE FROM files f WHERE f.id=%s AND f.vault_id=%s AND {_NO_HISTORY}",
                (row["id"], vault_id),
            )
            if dropped.rowcount != 1:
                continue
            mark_path_dirty(connection, vault_id, row["path"])
            retired += 1
