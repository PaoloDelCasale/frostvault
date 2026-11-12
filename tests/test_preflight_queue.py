"""#461 real PostgreSQL admission/single-flight/restart tests (CI only)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from app import main, storage
from app.catalog import ArchiveCatalog
from app.config import settings
from app.services import cloud_preflight_gate as gate, vault_relocation
from app.services.operation_policies import set_policy, OperationPolicy
from app.sessions import create_session, csrf_token_for
from tests.postgres import PostgresConnection, database_dsn, run_alembic
from tests.snapshot_s3_stub import VersionedS3Stub


class PreflightQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.database = self.base / 'catalog'
        migrated = run_alembic(self.database)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.enterContext(patch.object(gate, '_ready_pid', None))
        self.now = datetime.now(timezone.utc)
        self.observed = (self.now - timedelta(minutes=10)).isoformat()
        self.vaults = []
        with self.db() as c:
            c.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')")
            for i in (1, 2):
                root = self.base / str(i)
                root.mkdir()
                c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix,root_identity,root_identity_version) "
                          "VALUES (%s,%s,%s,%s,'bucket',%s,%s,%s)",
                          (i, f'vault-{i}', f'Vault {i}', str(root), f'vault-{i}', vault_relocation.root_identity(root), vault_relocation.ROOT_IDENTITY_VERSION))
                self.vaults.append(dict(c.execute('SELECT * FROM vaults WHERE id=%s', (i,)).fetchone()))
                for name in ('a.txt', 'nested/b.txt'):
                    file = root / name
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(b'photo')
                    os.utime(file, (1_500_000_000, 1_500_000_000))
                    stat = file.stat()
                    ArchiveCatalog(c).observe_local_copy(vault_id=i, path=name, file_type='regular', size=stat.st_size,
                                                         mtime_ns=stat.st_mtime_ns, observed_at=self.observed)
        configured = replace(settings, database_dsn=database_dsn(self.database), cookie_secure=False, operation_concurrency=16)
        for p in (patch('app.database.settings', configured), patch('app.main.settings', configured),
                  patch('app.sessions.settings', configured), patch('app.storage.settings', configured),
                  patch('app.storage.source_layout.vault_local_access', return_value=SimpleNamespace(
                      local_operations_allowed=True, volume_alias=None, volume_health='healthy'))):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app, client=('127.0.0.1', 50000))
        with self.db() as c:
            token = create_session(c, user_id=1, auth_method='oidc')
            csrf = csrf_token_for(c, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.headers = {'X-CSRF-Token': csrf}

    def db(self):
        return PostgresConnection(self.database)

    def fail_uploads(self, vault_id=None):
        with self.db() as c:
            for v in self.vaults:
                if vault_id is not None and v['id'] != vault_id:
                    continue
                catalog = ArchiveCatalog(c)
                ids, _, _ = catalog.queue_jobs(vault_id=v['id'], path='', action='upload', requested_by=1,
                                               requested_at=self.observed, group_id=f'old-{v["id"]}', is_directory=True,
                                               whole_vault=True, origin='automatic')
                for id in ids:
                    c.execute("UPDATE jobs SET status='failed' WHERE id=%s", (id,))

    def retry(self, vault_id=None):
        response = self.client.post('/api/upload/retry-failed' + (f'?vault_id={vault_id}' if vault_id else ''), headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['item_count']

    def test_invalid_preflight_keeps_jobs_and_budget_then_automatically_recovers(self):
        self.fail_uploads(1)
        self.assertEqual(self.retry(1), 2)
        provider = VersionedS3Stub(version_bytes=b'photo', head_object=lambda **kwargs: {
            'VersionId': kwargs.get('VersionId') or 'uploaded-1', 'ContentLength': 5,
            'StorageClass': 'STANDARD', 'ETag': '"etag"',
        })
        with patch('app.storage._validate_cloud_vault', side_effect=gate.ReadinessFailure('Grant s3:GetBucketPublicAccessBlock')) as probe, patch('app.storage.upload_s3_file') as upload:
            storage.process_jobs_once()
            storage.process_jobs_once()
            self.assertEqual(probe.call_count, 1)
            upload.assert_not_called()
        with self.db() as c:
            jobs = c.execute("SELECT * FROM jobs WHERE status<>'failed'").fetchall()
            self.assertTrue(all(j['status'] == 'queued' and j['retry_count'] == 0 for j in jobs))
            self.assertEqual(c.execute("SELECT count(*) AS n FROM notifications WHERE problem_status='open' AND event='cloud_preflight_blocked'").fetchone()['n'], 1)
            self.assertEqual(c.execute("SELECT vault_id FROM notifications WHERE event='cloud_preflight_blocked'").fetchone()['vault_id'], None)
            c.execute("UPDATE cloud_preflight_state SET next_probe_at='2000-01-01T00:00:00+00:00'")
        # Independent automatic probe, not a manual retry, also works without worker capacity.
        with patch('app.storage._validate_cloud_vault'), patch('app.storage.s3_client', return_value=provider), patch('app.storage.upload_s3_file', return_value={'VersionId': 'uploaded-1'}):
            storage.reprobe_cloud_readiness_once()
            storage.process_jobs_once()
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='completed'").fetchone()['n'], 2)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='failed'").fetchone()['n'], 2)  # preserved history
            self.assertEqual(c.execute("SELECT count(*) AS n FROM notifications WHERE event='cloud_preflight_blocked' AND problem_status='open'").fetchone()['n'], 0)

    def test_restart_ready_cache_bad_credentials_prevents_claim_and_dispatch(self):
        self.fail_uploads(1)
        self.retry(1)
        gate.check(self.db, lambda: None, now=self.now)
        gate._ready_pid = None  # The persisted state outlives the process.
        with patch('app.storage._validate_cloud_vault', side_effect=gate.ReadinessFailure('Check AWS credentials')), patch.object(
            ArchiveCatalog, 'claim_job', wraps=ArchiveCatalog.claim_job
        ) as claim, patch('app.storage.process_job') as dispatch:
            self.assertEqual(storage.process_jobs_once(), 0)
        claim.assert_not_called()
        dispatch.assert_not_called()
        with self.db() as c:
            self.assertEqual(gate.snapshot(c)['status'], 'blocked')
            jobs = c.execute("SELECT status,retry_count,claim_token FROM jobs WHERE status<>'failed'").fetchall()
            self.assertTrue(all(j == {'status': 'queued', 'retry_count': 0, 'claim_token': None} for j in jobs))

    def test_live_scheduler_does_not_wait_for_startup_provider_or_claim_jobs(self):
        self.fail_uploads(1)
        self.retry(1)
        entered,release = threading.Event(),threading.Event()
        def slow_probe(vault):
            entered.set()
            self.assertTrue(release.wait(10))
            raise gate.ReadinessFailure('Check credentials')
        with patch('app.storage._validate_cloud_vault',side_effect=slow_probe) as probe, patch(
            'app.storage.process_job'
        ) as dispatch, ThreadPoolExecutor(max_workers=1) as pool:
            checking = pool.submit(storage.reprobe_cloud_readiness_once)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(storage.process_jobs_once(wait=False),0)
                self.assertFalse(checking.done(),'Scheduler returned while provider remains blocked')
                self.assertEqual(probe.call_count,1)
                dispatch.assert_not_called()
                with self.db() as c:
                    self.assertEqual(c.execute("SELECT count(*) n FROM jobs WHERE status='queued' AND claim_token IS NULL").fetchone()['n'],2)
            finally:
                release.set()
            checking.result(timeout=5)

    def test_missing_automatic_copy_does_not_starve_healthy_other_vault(self):
        with self.db() as c:
            catalog = ArchiveCatalog(c)
            missing, _, _ = catalog.queue_jobs(
                vault_id=1, path='a.txt', action='upload', requested_by=1,
                requested_at=self.observed, group_id='missing', is_directory=False,
                origin='automatic',
            )
            healthy, _, _ = catalog.queue_jobs(
                vault_id=2, path='a.txt', action='upload', requested_by=1,
                requested_at=self.observed, group_id='healthy', is_directory=False,
            )
            c.execute("UPDATE local_copies SET presence='missing' WHERE vault_file_id="
                      "(SELECT vault_file_id FROM jobs WHERE id=%s)", (missing[0],))
        (self.base / '1' / 'a.txt').unlink()
        processed = []
        def finish(job):
            processed.append(job['id'])
            with self.db() as c:
                c.execute("UPDATE jobs SET status='completed' WHERE id=%s", (job['id'],))
        with patch('app.storage.validate_cloud_vault'), patch('app.storage.process_job', side_effect=finish):
            # The removed copy is still dispatched, but only so its worker can
            # close it as a removed file (#493, #499): it never starves the
            # healthy Vault and consumes no retry budget.
            self.assertEqual(storage.process_jobs_once(), 2)
            self.assertEqual(storage.process_jobs_once(), 0)
        self.assertEqual(sorted(processed), sorted([*healthy, *missing]))
        with self.db() as c:
            row = c.execute("SELECT retry_count FROM jobs WHERE id=%s", (missing[0],)).fetchone()
            self.assertEqual(row, {'retry_count': 0})

    def test_sixteen_simultaneous_auth_failures_wait_and_promptly_invalidate_ready(self):
        with self.db() as c:
            catalog = ArchiveCatalog(c)
            for i in range(16):
                file = self.base / '1' / f'auth-{i}.txt'
                file.write_bytes(b'photo')
                catalog.observe_local_copy(vault_id=1,path=file.name,file_type='regular',size=5,
                    mtime_ns=file.stat().st_mtime_ns,observed_at=self.observed)
                catalog.queue_jobs(vault_id=1,path=file.name,action='upload',requested_by=1,
                    requested_at=self.observed,group_id=f'auth-{i}',is_directory=False)
        barrier = threading.Barrier(16)
        def refused(job):
            barrier.wait(timeout=10)
            raise ClientError({'Error': {'Code': 'InvalidAccessKeyId', 'Message': 'not for display'},
                               'ResponseMetadata': {'HTTPStatusCode': 403}}, 'PutObject')
        with patch('app.storage._validate_cloud_vault'), patch('app.storage.process_upload', side_effect=refused):
            self.assertEqual(storage.process_jobs_once(), 16)
        with self.db() as c:
            state = gate.snapshot(c)
            self.assertEqual(state['status'], 'blocked')
            self.assertTrue(gate.probe_due(state, datetime.now(timezone.utc)))
            self.assertEqual(c.execute("SELECT count(*) n FROM jobs WHERE status='queued' AND retry_count=0 AND claim_token IS NULL").fetchone()['n'],16)
            self.assertEqual(c.execute("SELECT count(*) n FROM notifications WHERE event='job_failed'").fetchone()['n'],0)
            self.assertEqual(c.execute("SELECT count(*) n FROM notifications WHERE event='cloud_preflight_blocked' AND problem_status='open'").fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()').fetchone()['deadlocks'],0)
        with patch('app.storage._validate_cloud_vault') as probe:
            storage.reprobe_cloud_readiness_once()
            probe.assert_called_once()
        with self.db() as c:
            self.assertEqual(gate.snapshot(c)['status'], 'ready')

    def test_scans_and_audits_share_one_warning_without_suppressing_local_catalog(self):
        with patch('app.storage.runtime_status', {}), patch('app.storage._verified_local_scan', return_value=(2, {}, None, True)), patch('app.storage._validate_cloud_vault', side_effect=gate.ReadinessFailure('Grant s3:GetBucketPublicAccessBlock')):
            for vault in self.vaults:
                self.assertEqual(storage.scan_vault(vault)['local'], 2)
            storage.audit_all_vaults()
        with self.db() as c:
            rows = c.execute("SELECT event FROM notifications WHERE problem_status='open'").fetchall()
            self.assertEqual(rows, [{'event': 'cloud_preflight_blocked'}])

    def test_single_flight_sixteen_workers_restart_and_safe_remediation(self):
        entered, release = threading.Event(), threading.Event()
        count = []
        def probe():
            count.append(1)
            entered.set()
            self.assertTrue(release.wait(10))
            raise gate.ReadinessFailure('Grant s3:GetBucketPublicAccessBlock')
        with ThreadPoolExecutor(max_workers=16) as pool:
            first = pool.submit(gate.check, self.db, probe, now=self.now)
            self.assertTrue(entered.wait(5))
            others = [pool.submit(gate.check, self.db, probe, now=self.now) for _ in range(15)]
            for result in others:
                self.assertEqual(result.result(timeout=5)['status'], 'blocked')
            # Health/catalog readers remain responsive while provider is stalled.
            with self.db() as c:
                self.assertEqual(c.execute('SELECT count(*) AS n FROM vault_files').fetchone()['n'], 4)
            release.set()
            self.assertEqual(first.result()['status'], 'blocked')
        self.assertEqual(len(count), 1)
        # A real new process sees persisted backoff, not an in-memory cache.
        script = "from app.services.cloud_preflight_gate import check; from app.database import db; import json; print(json.dumps(check(db, lambda: (_ for _ in ()).throw(AssertionError('must not reprobe')))))"
        env = {**os.environ, 'DATABASE_URL': database_dsn(self.database)}
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['failure_count'], 1)
        with self.db() as c:
            c.execute("UPDATE cloud_preflight_state SET next_probe_at='2000-01-01T00:00:00+00:00'")
        state = gate.check(self.db, Mock(side_effect=RuntimeError('SECRET provider payload')), now=self.now)
        self.assertNotIn('SECRET', str(state))
        self.assertLessEqual((datetime.fromisoformat(state['next_probe_at']) - self.now).total_seconds(), 60)

    def test_retry_per_vault_global_idempotent_and_active_dedupe(self):
        self.fail_uploads()
        self.assertEqual(self.retry(1), 2)
        self.assertEqual(self.retry(1), 0)
        self.assertEqual(self.retry(), 2)
        self.assertEqual(self.retry(), 0)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='failed'").fetchone()['n'], 4)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='queued'").fetchone()['n'], 4)

    def test_concurrent_global_retry_admits_one_job_per_file(self):
        self.fail_uploads()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: main.retry_failed_uploads(vault_id=None, _={'id': 1}), range(2)))
        self.assertEqual(sum(r['item_count'] for r in results), 4)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='queued'").fetchone()['n'], 4)

    def test_disabled_stopped_relocated_suspended_conflict_cancelled_and_unstable_are_not_retried(self):
        self.fail_uploads()
        variants = ["UPDATE vaults SET enabled=FALSE", "UPDATE vaults SET decommission_state='decommissioning'",
                    "UPDATE vaults SET relocation_state='scanning'"]
        for sql in variants:
            with self.db() as c:
                c.execute(sql)
            self.assertEqual(self.retry(), 0)
            with self.db() as c:
                c.execute("UPDATE vaults SET enabled=TRUE,decommission_state='active',relocation_state='ready'")
        with self.db() as c:
            for v in (1, 2): set_policy(c, v, OperationPolicy(auto_upload=False))
        self.assertEqual(self.retry(), 0)
        with self.db() as c:
            for v in (1, 2): set_policy(c, v, OperationPolicy())
            c.execute("UPDATE jobs SET status='cancelled' WHERE vault_id=1")
            c.execute("UPDATE local_copies SET content_observed_at=%s WHERE vault_file_id IN (SELECT id FROM vault_files WHERE vault_id=2)", (self.now.isoformat(),))
        self.assertEqual(self.retry(), 0)
        with self.db() as c:
            c.execute("UPDATE local_copies SET content_observed_at=%s", (self.observed,))
            # Missing catalog Local Copies, source mutation and absent volume also fail closed.
            c.execute("UPDATE local_copies SET presence='missing'")
        self.assertEqual(self.retry(), 0)

    def test_protected_conflicted_and_other_operation_failures_are_never_retried(self):
        self.fail_uploads()
        with self.db() as c:
            catalog = ArchiveCatalog(c)
            for path in ('a.txt', 'nested/b.txt'):
                version = catalog.record_archive_version(vault_id=1, path=path, object_key='vault-1/' + path,
                    provider_version_id=path, size=5, storage_class='STANDARD', etag='etag',
                    uploaded_at=self.observed, observed_at=self.observed, scan_id='old')
                digest = hashlib.sha256(b'photo').hexdigest()
                catalog.mark_version_verified(version, plaintext_sha256=digest, verified_at=self.observed)
                if path == 'a.txt':
                    catalog.set_local_fingerprint(vault_id=1, path=path, plaintext_sha256=digest, matched_archive_version_id=version)
                else:
                    c.execute('UPDATE local_copies SET cloud_only_version_id=%s WHERE vault_file_id=(SELECT vault_file_id FROM file_paths WHERE vault_id=1 AND path=%s AND valid_to IS NULL)', (version, path))
            c.execute("UPDATE jobs SET action='recover' WHERE vault_id=2")
        self.assertEqual(self.retry(), 0)
        for action in ('free-space', 'cloud-purge'):
            with self.db() as c:
                c.execute('UPDATE jobs SET action=%s WHERE vault_id=2', (action,))
            self.assertEqual(self.retry(), 0)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='failed'").fetchone()['n'], 4)

    def test_explicit_retry_reuses_catalogued_unverified_version_without_new_put(self):
        self.fail_uploads(1)
        digest = hashlib.sha256(b'photo').hexdigest()
        with self.db() as c:
            catalog = ArchiveCatalog(c)
            for path in ('a.txt', 'nested/b.txt'):
                version = catalog.record_archive_version(vault_id=1, path=path, object_key='vault-1/' + path,
                    provider_version_id='uploaded-1', size=5, storage_class='STANDARD', etag='etag',
                    uploaded_at=self.observed, observed_at=self.observed, scan_id='old', origin='upload')
                c.execute("UPDATE jobs SET archive_version_id=%s, upload_plaintext_sha256=%s WHERE vault_id=1 AND path=%s", (version, digest, path))
        self.assertEqual(self.retry(1), 2)
        provider = VersionedS3Stub(version_bytes=b'photo', head_object=lambda **kwargs: {
            'VersionId': kwargs.get('VersionId') or 'uploaded-1', 'ContentLength': 5,
            'StorageClass': 'STANDARD', 'ETag': '"etag"',
        })
        with patch('app.storage.validate_cloud_vault'), patch('app.storage.s3_client', return_value=provider), patch('app.storage.upload_s3_file') as put:
            storage.process_jobs_once()
            put.assert_not_called()
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='completed'").fetchone()['n'], 2)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM jobs WHERE status='failed'").fetchone()['n'], 2)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM archive_versions WHERE integrity='verified'").fetchone()['n'], 2)
        self.assertTrue(all(read['VersionId'] == 'uploaded-1' for read in provider.reads))

    def test_source_volume_root_replacement_and_changed_content_fail_closed(self):
        self.fail_uploads()
        with patch('app.storage.source_layout.vault_local_access', return_value=SimpleNamespace(local_operations_allowed=False)):
            self.assertEqual(self.retry(), 0)
        for vault in self.vaults:
            root = Path(vault['source_root'])
            root.rename(root.with_name(root.name + '-old'))
            root.mkdir()
        self.assertEqual(self.retry(), 0)
        for vault in self.vaults:
            root = Path(vault['source_root'])
            root.rmdir()
            root.with_name(root.name + '-old').rename(root)
            for file in root.rglob('*.txt'):
                file.write_bytes(b'changed after catalog observation')
        self.assertEqual(self.retry(), 0)

    def test_gate_backoff_survives_connections_and_never_exceeds_sixty_seconds(self):
        current = self.now
        for index in range(9):
            state = gate.check(self.db, Mock(side_effect=gate.ReadinessFailure('Grant s3:GetBucketVersioning')), now=current)
            due = datetime.fromisoformat(state['next_probe_at'])
            self.assertGreater((due - current).total_seconds(), 0)
            self.assertLessEqual((due - current).total_seconds(), 60)
            current = due
        state = gate.check(self.db, Mock(), now=current)
        self.assertEqual(state['status'], 'ready')
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM notifications WHERE event='cloud_preflight_blocked'").fetchone()['n'], 1)

    def test_migration_is_metadata_only_and_preserves_attempt_history(self):
        self.fail_uploads()
        result = run_alembic(self.database, '0061_aggregate_dirty_journal', 'downgrade')
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.db() as c:
            before = c.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        result = run_alembic(self.database)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.db() as c:
            from tests.upload_verification_migration_contract import without_upload_verification_fields
            self.assertEqual(without_upload_verification_fields(c.execute('SELECT * FROM jobs ORDER BY id').fetchall(), 'jobs'), before)
            self.assertEqual(gate.snapshot(c)['status'], 'unknown')

    def test_whole_upload_root_is_narrow_and_auth_csrf_preserved(self):
        response = self.client.post('/api/upload?vault_id=1', json={'path': '', 'is_directory': True}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['item_count'], 0)
        self.assertEqual(response.json()['admission']['selected_count'], 2)
        self.assertEqual(response.json()['admission']['status'], 'pending')
        from app.services import bulk_upload_admission
        bulk_upload_admission.tick(self.db)
        with self.db() as c:
            self.assertEqual(bulk_upload_admission.get(c, vault_id=1, group_id=response.json()['group_id'])['admitted_count'], 2)
        for path in ('', '.', '..', '/', '../outside', 'nested/../../outside'):
            response = self.client.post('/api/upload?vault_id=2', json={'path': path}, headers=self.headers)
            self.assertIn(response.status_code, (400, 422), response.text)
        for action in ('recover', 'free-space'):
            response = self.client.post('/api/' + action + '?vault_id=2', json={'path': '', 'is_directory': True}, headers=self.headers)
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.client.post('/api/upload/retry-failed').status_code, 403)
        self.client.cookies.clear()
        self.assertIn(self.client.post('/api/upload/retry-failed', headers=self.headers).status_code, (401, 403))
