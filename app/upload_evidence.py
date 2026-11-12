"""Native evidence component; runtime/job/provider cutover is still pending.

Expected bytes/layout are stored once. Attempts refer to them and freeze typed
namespace/root inputs, without a Job FK or repeated target/snapshot JSON. Receipt
append owns a separate transaction and never asks for a live claim. All other
methods are DB-only units owned by the caller; none commits or retries effects.
These metadata methods do NOT grant a lease, a live root, or deletion consent.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb

from .database import transaction
from .file_state import FileStateConflict, FileStateStore, _timestamp
from .services.upload_provenance import receipt_identity
from .services.upload_snapshot import UploadSnapshot, check_s3_proof
from .services.vault_paths import expected_cloud_key


class EvidenceConflict(RuntimeError):
    """Evidence is contradictory, superseded or insufficient; do not send again."""


def decode_snapshot(row: dict[str, Any]) -> UploadSnapshot:
    snapshot = UploadSnapshot(row['size'], row['plaintext_sha256'].hex(),
        row['checksum_type'], row['checksum_sha256'], row['part_size'],
        tuple(base64.b64encode(part).decode('ascii') for part in row['part_checksums']), row['mtime_ns'])
    # One existing cryptographic/layout validator; no native-vs-legacy modes.
    return UploadSnapshot.from_json(snapshot.as_json())


def append_receipt(connection_factory: Callable[[], Any], *, attempt_id: UUID,
                   response: dict[str, Any], at: datetime) -> dict[str, Any]:
    """Commit an allowlisted fact independently, including malformed/late ACKs.

    Return only after COMMIT. A commit error must retain/retry this fact, NEVER
    replay PUT. No lease, File/Vault lock, current Job or receiver batch required.
    """
    _timestamp(at)
    text, fingerprint = receipt_identity(response)
    fact = json.loads(text)
    version = fact.get('VersionId')
    if not isinstance(version, str) or version in ('', 'null'):
        version = None
    with connection_factory() as connection:
        row = connection.execute('''INSERT INTO upload_receipt_facts(
                attempt_id,receipt_hash,provider_version_id,response,received_at)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (attempt_id,receipt_hash) DO NOTHING RETURNING *''',
            (attempt_id, bytes.fromhex(fingerprint), version, Jsonb(fact), at)).fetchone()
        if row is None:
            row = connection.execute('SELECT * FROM upload_receipt_facts WHERE attempt_id=%s AND receipt_hash=%s',
                                     (attempt_id,bytes.fromhex(fingerprint))).fetchone()
    return row


class UploadEvidenceStore:
    def __init__(self, connection: Any):
        self.connection = connection
        self.files = FileStateStore(connection)

    def freeze(self, *, vault_id: int, file_id: UUID, snapshot: UploadSnapshot,
               at: datetime) -> dict[str, Any]:
        _timestamp(at)
        if snapshot.mtime_ns is None:
            raise ValueError('The new baseline requires original nanosecond mtime')
        if type(snapshot.size) is not int or type(snapshot.part_size) is not int:
            raise ValueError('Expected size/layout must be integers')
        validated = UploadSnapshot.from_json(snapshot.as_json())
        # Normalize textual digest encodings before content-addressing evidence.
        normalized = UploadSnapshot(validated.size, bytes.fromhex(validated.plaintext_sha256).hex(),
            validated.checksum_type,validated.checksum_sha256,validated.part_size,
            tuple(base64.b64encode(base64.b64decode(p,validate=True)).decode('ascii')
                  for p in validated.part_checksums),validated.mtime_ns)
        fingerprint = hashlib.sha256(normalized.as_json().encode()).digest()
        self.files._lock_vault(vault_id)
        if self.files.get(vault_id=vault_id, file_id=file_id) is None:
            raise FileStateConflict('File no longer belongs to this Vault')
        row = self.connection.execute('''INSERT INTO upload_snapshots(
                vault_file_id,evidence_hash,size,plaintext_sha256,mtime_ns,checksum_type,
                checksum_sha256,part_size,part_checksums,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vault_file_id,evidence_hash) DO NOTHING RETURNING *''',
            (file_id,fingerprint,normalized.size,bytes.fromhex(normalized.plaintext_sha256),
             normalized.mtime_ns,normalized.checksum_type,normalized.checksum_sha256,
             normalized.part_size,[base64.b64decode(p,validate=True) for p in normalized.part_checksums],at)).fetchone()
        if row is None:
            row = self.connection.execute('SELECT * FROM upload_snapshots WHERE vault_file_id=%s AND evidence_hash=%s',
                                          (file_id,fingerprint)).fetchone()
        if decode_snapshot(row).as_json() != normalized.as_json():
            raise EvidenceConflict('Stored expected evidence contradicts its identity')
        return row

    def prepare(self, *, vault_id: int, file_id: UUID, snapshot_id: UUID, provider: str,
                verification_mode: str, storage_class: str, at: datetime) -> dict[str, Any]:
        """Freeze scope AFTER the Vault lock; never coobserve stale joined state while waiting."""
        _timestamp(at)
        self.files._lock_vault(vault_id)
        target = self.connection.execute('''SELECT f.*,v.source_root,v.root_identity,
                v.root_identity_version,v.s3_bucket,v.s3_prefix,v.enabled,
                v.decommission_state,v.relocation_state,v.root_released_at
            FROM file_state f JOIN vaults v ON v.id=f.vault_id
            WHERE f.id=%s AND f.vault_id=%s''', (file_id,vault_id)).fetchone()
        if (target is None or target['status'] != 'active' or not target['enabled']
                or target['decommission_state'] != 'active' or target['relocation_state'] != 'ready' or target['root_released_at'] is not None
                or not target['root_identity'] or not target['root_identity_version']):
            raise FileStateConflict('Active enrolled source is required')
        expected = self.connection.execute('SELECT * FROM upload_snapshots WHERE id=%s AND vault_file_id=%s',
                                           (snapshot_id,file_id)).fetchone()
        if expected is None:
            raise EvidenceConflict('Expected evidence belongs to another File')
        if target['pending_snapshot_id'] is not None:
            raise EvidenceConflict('An exact candidate already needs reconciliation')
        if target['held_snapshot_id'] is not None:
            held = self.connection.execute('''SELECT s.plaintext_sha256 FROM archive_objects a
                JOIN upload_attempts t ON t.id=a.attempt_id JOIN upload_snapshots s ON s.id=t.snapshot_id
                WHERE a.id=%s AND a.vault_file_id=%s''', (target['held_snapshot_id'],file_id)).fetchone()
            if held is None or held['plaintext_sha256'] != expected['plaintext_sha256']:
                raise EvidenceConflict('Changed reappearance requires explicit Owner consent')
        key = expected_cloud_key(target['current_path'], target['s3_prefix'])
        existing = self.connection.execute('SELECT * FROM upload_attempts WHERE vault_file_id=%s AND settled_at IS NULL',
                                           (file_id,)).fetchone()
        scope = dict(snapshot_id=snapshot_id,provider=provider,bucket=target['s3_bucket'],object_key=key,
            path_generation=target['path_generation'],source_root=target['source_root'],
            root_identity=target['root_identity'],root_identity_version=target['root_identity_version'],
            expected_current_id=target['current_snapshot_id'],verification_mode=verification_mode,storage_class=storage_class)
        if existing is not None:
            if all(existing[field] == value for field,value in scope.items()):
                return existing  # send_started_at still controls admission, not the mere return value.
            raise EvidenceConflict('An earlier attempt must be settled, never superseded implicitly')
        columns = ','.join(scope)
        values = ','.join(['%s'] * len(scope))
        return self.connection.execute(f'''INSERT INTO upload_attempts(vault_file_id,{columns},created_at)
            VALUES (%s,{values},%s) RETURNING *''', (file_id,*scope.values(),at)).fetchone()

    def start_send(self, *, vault_id: int, file_id: UUID, attempt_id: UUID,
                   at: datetime) -> dict[str, Any] | None:
        """Metadata CAS only. Caller MUST commit before any effect and retain live claim/root guards."""
        _timestamp(at)
        self.files._lock_vault(vault_id)
        return self.connection.execute('''UPDATE upload_attempts t SET send_started_at=%s
            FROM file_state f JOIN vaults v ON v.id=f.vault_id
            WHERE t.id=%s AND t.vault_file_id=%s AND f.id=t.vault_file_id AND f.vault_id=%s
              AND t.send_started_at IS NULL AND t.settled_at IS NULL
              AND f.status='active' AND f.pending_snapshot_id IS NULL
              AND t.path_generation=f.path_generation
              AND t.expected_current_id IS NOT DISTINCT FROM f.current_snapshot_id
              AND t.source_root=v.source_root AND t.root_identity=v.root_identity
              AND t.root_identity_version=v.root_identity_version
              AND t.bucket=v.s3_bucket AND t.object_key=%s
              AND v.enabled AND v.decommission_state='active' AND v.relocation_state='ready'
              AND v.root_released_at IS NULL
            RETURNING t.*''', (at,attempt_id,file_id,vault_id,self._current_key(vault_id,file_id))).fetchone()

    def _current_key(self, vault_id: int, file_id: UUID) -> str | None:
        row = self.connection.execute('''SELECT f.current_path,v.s3_prefix FROM file_state f
            JOIN vaults v ON v.id=f.vault_id WHERE f.id=%s AND f.vault_id=%s''', (file_id,vault_id)).fetchone()
        return expected_cloud_key(row['current_path'],row['s3_prefix']) if row and row['current_path'] else None

    def materialize(self, *, vault_id: int, file_id: UUID, attempt_id: UUID) -> dict[str, Any]:
        """Bind exactly one usable receipt, without verification or publication/election."""
        self.files._lock_vault(vault_id)
        target = self.files.get(vault_id=vault_id,file_id=file_id)
        attempt = self.connection.execute('SELECT * FROM upload_attempts WHERE id=%s AND vault_file_id=%s',
                                          (attempt_id,file_id)).fetchone()
        facts = self.connection.execute('SELECT * FROM upload_receipt_facts WHERE attempt_id=%s', (attempt_id,)).fetchall()
        if (target is None or target['status'] != 'active' or attempt is None or attempt['send_started_at'] is None
                or attempt['settled_at'] is not None or len(facts) != 1 or facts[0]['provider_version_id'] is None
                or target['path_generation'] != attempt['path_generation']):
            raise EvidenceConflict('Exact open attempt and one usable receipt required')
        fact = facts[0]
        if target['current_snapshot_id'] is not None:
            published = self.connection.execute('SELECT * FROM archive_objects WHERE id=%s AND receipt_id=%s',
                (target['current_snapshot_id'],fact['id'])).fetchone()
            if published is not None:
                return published  # Exact already-committed publication, not historical election.
        if target['current_snapshot_id'] != attempt['expected_current_id']:
            raise EvidenceConflict('Current Snapshot changed')
        if target['pending_snapshot_id'] is not None:
            existing = self.connection.execute('SELECT * FROM archive_objects WHERE id=%s AND receipt_id=%s',
                (target['pending_snapshot_id'],fact['id'])).fetchone()
            if existing is None:
                raise EvidenceConflict('A different exact candidate already exists')
            return existing
        candidate = self.connection.execute('''INSERT INTO archive_objects(
            vault_file_id,attempt_id,receipt_id,provider,bucket,object_key,provider_version_id,storage_class)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (file_id,attempt_id,fact['id'],attempt['provider'],attempt['bucket'],attempt['object_key'],
             fact['provider_version_id'],attempt['storage_class'])).fetchone()
        self.connection.execute('UPDATE file_state SET pending_snapshot_id=%s WHERE id=%s AND vault_id=%s',
                                (candidate['id'],file_id,vault_id))
        return candidate

    def publish_verified(self, *, vault_id: int, file_id: UUID, object_id: UUID,
                         digest: bytes, method: str, at: datetime,
                         read_back_size: int | None = None,
                         observed_proof: dict[str, Any] | None = None,
                         observation_generation: UUID | None = None) -> dict[str, Any]:
        """One caller-owned atomic verification/pointer/local-generation unit.

        Effects and hashing must already be finished OUTSIDE this transaction.
        The integration must retain its live claim/root checks and final fenced
        Job status CAS in the SAME outer transaction. No SHARE-to-exclusive
        upgrade: start the catalog unit with its exclusive Vault capability.
        """
        _timestamp(at)
        if method not in ('s3_checksum','read_back') or not isinstance(digest,bytes) or len(digest)!=32:
            raise ValueError('Exact supported verification proof is required')
        with transaction(self.connection):
            self.files._lock_vault(vault_id)
            row = self.connection.execute('''SELECT a.*,t.snapshot_id,t.path_generation,
                    t.expected_current_id,t.source_root,t.root_identity,t.root_identity_version,
                    t.send_started_at,t.settled_at,r.response,
                    f.current_snapshot_id,f.pending_snapshot_id,f.path_generation AS live_generation,
                    v.source_root AS live_root,v.root_identity AS live_identity,
                    v.root_identity_version AS live_identity_version,v.s3_bucket AS live_bucket,
                    v.enabled,v.decommission_state,v.relocation_state,v.root_released_at
                FROM archive_objects a JOIN upload_attempts t ON t.id=a.attempt_id
                JOIN upload_receipt_facts r ON r.id=a.receipt_id
                JOIN file_state f ON f.id=a.vault_file_id JOIN vaults v ON v.id=f.vault_id
                WHERE a.id=%s AND a.vault_file_id=%s AND f.vault_id=%s AND f.status='active' ''',
                (object_id,file_id,vault_id)).fetchone()
            already_published = row is not None and row['current_snapshot_id'] == object_id
            if (row is None or (not already_published and row['pending_snapshot_id'] != object_id)
                    or row['availability'] != 'available'
                    or row['integrity'] != ('verified' if already_published else 'unverified')
                    or row['path_generation'] != row['live_generation']
                    or (not already_published and row['expected_current_id'] != row['current_snapshot_id'])
                    or row['send_started_at'] is None or row['settled_at'] is not None
                    or row['source_root'] != row['live_root'] or row['root_identity'] != row['live_identity']
                    or row['root_identity_version'] != row['live_identity_version']
                    or row['bucket'] != row['live_bucket'] or row['object_key'] != self._current_key(vault_id,file_id)
                    or not row['enabled'] or row['decommission_state'] != 'active' or row['relocation_state'] != 'ready'
                    or row['root_released_at'] is not None):
                raise EvidenceConflict('Candidate no longer belongs to the exact live metadata scope')
            facts = self.connection.execute('SELECT count(*) n FROM upload_receipt_facts WHERE attempt_id=%s',
                                            (row['attempt_id'],)).fetchone()['n']
            expected = self.connection.execute('SELECT * FROM upload_snapshots WHERE id=%s', (row['snapshot_id'],)).fetchone()
            snapshot = decode_snapshot(expected)
            if facts != 1 or digest != expected['plaintext_sha256']:
                raise EvidenceConflict('Contradictory receipt or verification digest')
            # Present contradictory checksum evidence may never become a
            # read-back downgrade. Missing proof alone permits real read-back.
            checksum = check_s3_proof(row['response'],
                version_id=row['provider_version_id'],snapshot=snapshot,completion=True)
            if observed_proof is not None:
                observed_checksum = check_s3_proof(observed_proof,
                    version_id=row['provider_version_id'],snapshot=snapshot)
                checksum = checksum or observed_checksum
            if (method == 's3_checksum' and not checksum) or (method == 'read_back' and
                    (type(read_back_size) is not int or read_back_size != snapshot.size)):
                raise EvidenceConflict('Verification requires exact checksum or complete read-back')
            if already_published:
                if row['verified_sha256'] != digest:
                    raise EvidenceConflict('Committed verification contradicts repeated publication')
                return self.files.get(vault_id=vault_id,file_id=file_id)
            self.connection.execute('''UPDATE archive_objects SET integrity='verified',
                verification_method=%s,verified_sha256=%s,verified_at=%s,verification_egress_bytes=%s
                WHERE id=%s''', (method,digest,at,read_back_size if method=='read_back' else 0,object_id))
            current = self.connection.execute('''UPDATE file_state SET current_snapshot_id=%s,pending_snapshot_id=NULL
                WHERE id=%s AND vault_id=%s AND pending_snapshot_id=%s
                    AND current_snapshot_id IS NOT DISTINCT FROM %s AND path_generation=%s RETURNING *''',
                (object_id,file_id,vault_id,object_id,row['expected_current_id'],row['path_generation'])).fetchone()
            if current is None:
                raise EvidenceConflict('Publication generation changed')
            if observation_generation is not None:
                updated = self.files.publish_digest(vault_id=vault_id,file_id=file_id,
                    path_generation=row['path_generation'],observation_generation=observation_generation,digest=digest)
                if updated is not None:
                    current = updated
            return current

    def current(self, *, vault_id: int, file_id: UUID, recoverable_only: bool = False) -> dict[str, Any] | None:
        """Only the explicit pointer, NEVER latest verified/available/history fallback.

        Contradictory late acknowledgments remain stored and fail closed for
        recovery. A stored pointer is metadata, not permission for provider I/O.
        """
        eligibility = '''AND a.integrity='verified' AND a.availability='available'
            AND a.verified_sha256=s.plaintext_sha256
            AND (SELECT count(*) FROM upload_receipt_facts r WHERE r.attempt_id=t.id)=1''' if recoverable_only else ''
        return self.connection.execute(f'''SELECT a.*,s.size,s.mtime_ns,s.plaintext_sha256
            FROM file_state f JOIN archive_objects a ON a.id=f.current_snapshot_id AND a.vault_file_id=f.id
            JOIN upload_attempts t ON t.id=a.attempt_id JOIN upload_snapshots s ON s.id=t.snapshot_id
            WHERE f.id=%s AND f.vault_id=%s AND f.status='active' {eligibility}''', (file_id,vault_id)).fetchone()
