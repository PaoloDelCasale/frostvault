"""Native current-file metadata for the incompatible baseline (cutover pending).

One row owns identity, current path and Local Copy observation. Closed mappings
are history only. This Module performs no filesystem/provider I/O, commits or
retries: the caller owns the transaction and its complete DB-only replay.

This is NOT cloud authority. Scoped Current Snapshot/evidence references and
existing claim/root/effect guards must be integrated before runtime cutover.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID


class FileStateConflict(RuntimeError):
    """The requested Vault/path/generation no longer names this active File."""


def _timestamp(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An aware observation timestamp is required')


def _path(value: str) -> None:
    if '\x00' in value or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('A normalized relative file path is required')


class FileStateStore:
    def __init__(self, connection: Any):
        self.connection = connection

    def _lock_vault(self, vault_id: int) -> None:
        # Same Vault-first order as the catalog/admission writers. There is no
        # implicit commit or caller-supplied skip-lock/authority flag.
        row = self.connection.execute('SELECT id FROM vaults WHERE id=%s FOR NO KEY UPDATE',
                                      (vault_id,)).fetchone()
        if row is None:
            raise FileStateConflict('Vault no longer exists')

    def get(self, *, vault_id: int, file_id: UUID) -> dict[str, Any] | None:
        """Return native values, not a reusable lease/root/cloud capability."""
        return self.connection.execute('SELECT * FROM file_state WHERE id=%s AND vault_id=%s',
                                       (file_id, vault_id)).fetchone()

    def create_identity(self, *, vault_id: int, path: str, at: datetime) -> dict[str, Any]:
        _path(path)
        _timestamp(at)
        self._lock_vault(vault_id)
        row = self.connection.execute('''INSERT INTO file_state(vault_id,current_path,path_since,created_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (vault_id,current_path) WHERE current_path IS NOT NULL DO NOTHING
            RETURNING *''', (vault_id, path, at, at)).fetchone()
        if row is None:
            row = self.connection.execute('SELECT * FROM file_state WHERE vault_id=%s AND current_path=%s',
                                          (vault_id, path)).fetchone()
        if row is None or row['status'] != 'active':
            raise FileStateConflict('Current path is unavailable')
        return row

    def observe(self, *, vault_id: int, path: str, file_type: str,
                size: int | None, mtime_ns: int | None, at: datetime,
                seen_at: datetime | None = None, recovered_at: datetime | None = None) -> dict[str, Any]:
        _path(path)
        _timestamp(at)
        _timestamp(seen_at or at)
        if recovered_at is not None:
            _timestamp(recovered_at)
        if file_type not in {'regular', 'directory', 'symlink', 'special'}:
            raise ValueError('Unsupported local entry type')
        if size is not None and size < 0:
            raise ValueError('Size must not be negative')
        presence = 'present' if file_type == 'regular' else 'unsupported'
        self._lock_vault(vault_id)
        row = self.connection.execute('''INSERT INTO file_state(
                vault_id,current_path,path_since,created_at,observation_generation,
                local_presence,local_type,local_size,local_mtime_ns,
                local_last_seen_at,local_observed_at,local_content_observed_at,local_recovered_at)
            VALUES (%s,%s,%s,%s,gen_random_uuid(),%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vault_id,current_path) WHERE current_path IS NOT NULL DO UPDATE SET
                observation_generation=excluded.observation_generation,
                local_presence=excluded.local_presence,local_type=excluded.local_type,
                local_size=excluded.local_size,local_mtime_ns=excluded.local_mtime_ns,
                local_sha256=CASE WHEN
                    (file_state.local_type,file_state.local_size,file_state.local_mtime_ns)
                    IS NOT DISTINCT FROM (excluded.local_type,excluded.local_size,excluded.local_mtime_ns)
                    THEN file_state.local_sha256 ELSE NULL END,
                matched_snapshot_id=CASE WHEN
                    (file_state.local_type,file_state.local_size,file_state.local_mtime_ns)
                    IS NOT DISTINCT FROM (excluded.local_type,excluded.local_size,excluded.local_mtime_ns)
                    THEN file_state.matched_snapshot_id ELSE NULL END,
                held_snapshot_id=CASE WHEN excluded.local_presence='present'
                    AND file_state.local_presence IS DISTINCT FROM 'present'
                    THEN CASE WHEN excluded.local_recovered_at IS NOT NULL THEN NULL ELSE
                        (SELECT a.id FROM archive_objects a WHERE a.id=file_state.current_snapshot_id
                         AND a.integrity='verified' AND a.availability='available') END
                    ELSE file_state.held_snapshot_id END,
                local_last_seen_at=excluded.local_last_seen_at,
                local_observed_at=excluded.local_observed_at,
                local_content_observed_at=CASE WHEN
                    (file_state.local_presence,file_state.local_type,file_state.local_size,file_state.local_mtime_ns)
                    IS DISTINCT FROM
                    (excluded.local_presence,excluded.local_type,excluded.local_size,excluded.local_mtime_ns)
                    THEN excluded.local_observed_at
                    ELSE COALESCE(file_state.local_content_observed_at,excluded.local_observed_at) END,
                local_recovered_at=COALESCE(excluded.local_recovered_at,file_state.local_recovered_at)
            WHERE file_state.status='active'
            RETURNING *''', (vault_id,path,at,at,presence,file_type,size,mtime_ns,
                             seen_at or at,at,at,recovered_at)).fetchone()
        if row is None:
            raise FileStateConflict('Current path is unavailable')
        return row

    def publish_digest(self, *, vault_id: int, file_id: UUID,
                       path_generation: UUID, observation_generation: UUID,
                       digest: bytes) -> dict[str, Any] | None:
        """Publish a fingerprint only for the exact observed path + generation.

        Even another hash publication invalidates this generation. This does
        not mark any cloud object verified or elect a Current Snapshot.
        """
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError('A 32-byte SHA-256 digest is required')
        self._lock_vault(vault_id)
        return self.connection.execute('''UPDATE file_state
            SET local_sha256=%s,observation_generation=gen_random_uuid(),
                matched_snapshot_id=(SELECT a.id FROM archive_objects a
                    JOIN upload_attempts t ON t.id=a.attempt_id JOIN upload_snapshots s ON s.id=t.snapshot_id
                    WHERE a.id=file_state.current_snapshot_id AND a.integrity='verified'
                      AND a.availability='available' AND s.plaintext_sha256=%s),
                held_snapshot_id=CASE WHEN EXISTS (SELECT 1 FROM archive_objects a
                    JOIN upload_attempts t ON t.id=a.attempt_id JOIN upload_snapshots s ON s.id=t.snapshot_id
                    WHERE a.id=file_state.held_snapshot_id AND s.plaintext_sha256=%s)
                    THEN NULL ELSE held_snapshot_id END
            WHERE id=%s AND vault_id=%s AND status='active'
              AND path_generation=%s AND observation_generation=%s
              AND local_presence='present' AND local_type='regular'
            RETURNING *''', (digest,digest,digest,file_id,vault_id,path_generation,observation_generation)).fetchone()

    def mark_missing(self, *, vault_id: int, file_id: UUID, path_generation: UUID,
                     observation_generation: UUID, at: datetime) -> dict[str, Any] | None:
        """CAS a real prior observation to Missing; absence is not an all-null copy."""
        _timestamp(at)
        self._lock_vault(vault_id)
        return self.connection.execute('''UPDATE file_state SET
            local_presence='missing',local_observed_at=%s,observation_generation=gen_random_uuid(),
            local_content_observed_at=CASE WHEN local_presence IS DISTINCT FROM 'missing'
                THEN %s ELSE local_content_observed_at END
            WHERE id=%s AND vault_id=%s AND status='active' AND path_generation=%s
                AND observation_generation=%s RETURNING *''',
            (at,at,file_id,vault_id,path_generation,observation_generation)).fetchone()

    def rename(self, *, vault_id: int, file_id: UUID, path_generation: UUID,
               new_path: str, at: datetime) -> dict[str, Any]:
        """Move one identity's path; not automatic two-File rename confirmation.

        Candidate merging still needs its full proof/pending-work guards at
        integration. Uniqueness errors abort the caller's transaction; a stale
        generation changes nothing, even if the caller catches the conflict.
        """
        _path(new_path)
        _timestamp(at)
        self._lock_vault(vault_id)
        previous = self.connection.execute('''SELECT * FROM file_state
            WHERE id=%s AND vault_id=%s AND path_generation=%s AND status='active'
            FOR UPDATE''', (file_id,vault_id,path_generation)).fetchone()
        if previous is None:
            raise FileStateConflict('Path generation changed')
        if previous['current_path'] == new_path:
            return previous
        moved = self.connection.execute('''UPDATE file_state SET
            current_path=%s,path_since=%s,path_generation=gen_random_uuid()
            WHERE id=%s AND vault_id=%s AND path_generation=%s
            RETURNING *''', (new_path,at,file_id,vault_id,path_generation)).fetchone()
        self.connection.execute('''INSERT INTO file_path_history(
            vault_file_id,vault_id,path,path_generation,valid_from,valid_to)
            VALUES (%s,%s,%s,%s,%s,%s)''', (file_id,vault_id,previous['current_path'],
                previous['path_generation'],previous['path_since'],at))
        return moved

    def history(self, *, vault_id: int, file_id: UUID) -> list[dict[str, Any]]:
        """Closed mappings only; timestamps are not cloud selection authority."""
        return self.connection.execute('''SELECT * FROM file_path_history
            WHERE vault_file_id=%s AND vault_id=%s ORDER BY id''', (file_id,vault_id)).fetchall()
