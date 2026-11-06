"""Conservative metadata-only repair of historical managed-download residues.

Ordinary *.tmp.* user names are never reserved. Legacy, non-reserved names need
an exact completed Recover history, size and creation-window correlation. No
filesystem content, cloud Versions or Job history is removed.
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


def retire_absent_restore_ghosts(connection: Any, vault_id: int, root: Path, at: str) -> int:
    """Idempotent, bounded, under the caller's normal local scan/mutation gate."""
    after = ""
    retired = 0
    while True:
        rows = connection.execute("""
            SELECT vf.id, fp.path, vf.created_at, lc.size
            FROM vault_files vf
            JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            JOIN local_copies lc ON lc.vault_file_id=vf.id
            WHERE vf.vault_id=%s AND vf.status='active' AND fp.path>%s AND fp.path ~ %s
              AND NOT EXISTS (SELECT 1 FROM archive_versions av WHERE av.vault_file_id=vf.id)
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.vault_file_id=vf.id)
              AND (SELECT count(*) FROM file_paths history WHERE history.vault_file_id=vf.id)=1
            ORDER BY fp.path LIMIT %s
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
                    JOIN archive_versions av ON av.id=j.archive_version_id AND av.vault_file_id=j.vault_file_id
                    WHERE j.vault_id=%s AND j.path=%s AND j.action='recover' AND j.status='completed'
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
            # Keep both the retired identity and Path History for auditability.
            # A later real user file at this name creates a fresh active identity.
            connection.execute("UPDATE vault_files SET status='retired', retired_at=%s WHERE id=%s", (at, row["id"]))
            connection.execute("UPDATE file_paths SET valid_to=%s WHERE vault_file_id=%s AND valid_to IS NULL", (at, row["id"]))
            mark_path_dirty(connection, vault_id, row["path"])
            retired += 1
