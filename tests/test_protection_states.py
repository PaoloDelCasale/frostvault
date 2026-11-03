"""Five protection observations; PostgreSQL parity runs only in PR CI."""
from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import settings
from app.main import list_files, stats
from app.services import directory_aggregates as aggregates
from app.services.protection_states import aggregate_state, observation_is_unconfirmed, project_file
from app.services.source_layout import VaultLocalAccess


class ProtectionProjectionTests(unittest.TestCase):
    def test_source_absent_preserves_last_evidence_and_date(self):
        for state in ('pending', 'protected', 'problem', 'unconfirmed', 'cloud_only'):
            row = {'protection_state': state, 'protection_observed_at': '2026-11-03T10:00:00Z'}
            with self.subTest(state=state):
                result = project_file(row, unconfirmed=True)
                self.assertEqual(result['state'], 'unconfirmed')
                self.assertEqual(result['last_known_state'], state)
                self.assertEqual(result['last_known_at'], row['protection_observed_at'])
                self.assertEqual(row['protection_state'], state)

    def test_pending_indicates_protected_previous_snapshot(self):
        result = project_file({'protection_state': 'pending', 'integrity': 'verified', 'availability': 'available'})
        self.assertTrue(result['previous_snapshot_protected'])

    def test_unknown_cloud_keeps_last_verified_state(self):
        result = project_file({'protection_state': 'unconfirmed', 'integrity': 'verified',
            'local_presence': 'present', 'matched_archive_version_id': 'v1',
            'archive_version_id': 'v1', 'local_sha256': 'a', 'version_sha256': 'a'})
        self.assertEqual(result['last_known_state'], 'protected')

    def test_aggregate_is_actionable_and_empty_is_not_protected(self):
        self.assertIsNone(aggregate_state({}))
        self.assertEqual(aggregate_state({'protected': 100, 'pending': 1}), 'pending')
        self.assertEqual(aggregate_state({'problem': 1, 'unconfirmed': 100}), 'problem')
        self.assertEqual(aggregate_state({'cloud_only': 2}), 'cloud_only')

    def test_only_failed_cloud_requests_make_observation_unknown(self):
        self.assertFalse(observation_is_unconfirmed({'audit': {'healthy': False}}))
        for component in ('cloud', 'audit'):
            self.assertTrue(observation_is_unconfirmed({component: {'last_error': 'offline'}}))
        self.assertFalse(observation_is_unconfirmed({'policy': {'last_error': 'blocked'}}))


class ProtectionStatePostgresTests(unittest.TestCase):
    def setUp(self):
        from tests.postgres import PostgresConnection, database_dsn
        from tests.test_database import run_alembic
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'protection.db'
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = PostgresConnection(str(self.path))
        self.connection.__enter__()
        self.addCleanup(self.connection.__exit__, None, None, None)
        self.connection.execute("INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (9, 'files', 'Files', '/source', 'bucket', 'files')")
        self.catalog = ArchiveCatalog(self.connection)
        self.cfg = replace(settings, database_dsn=database_dsn(str(self.path)))
        aggregates.set_maintenance_scheduling_enabled(False)
        self.addCleanup(aggregates.set_maintenance_scheduling_enabled, True)

    def seed(self, name, *, local=True, archive=False, verified=False, availability='available', match=True):
        path = f'photos/{name}.jpg'
        file_id = None
        if local:
            file_id = self.catalog.observe_local_copy(vault_id=9, path=path, file_type='regular', size=12, mtime_ns=1, observed_at='2026-11-03T10:00:00Z')
        version_id = None
        if archive:
            version_id = self.catalog.record_archive_version(vault_id=9, path=path, object_key=f'files/{path}', provider_version_id=name, size=12, storage_class='STANDARD', etag='e', uploaded_at='2026-11-03T10:00:00Z', observed_at='2026-11-03T10:00:00Z', scan_id='scan')
            self.connection.execute("UPDATE archive_versions SET integrity=%s, availability=%s, plaintext_sha256=%s, verified_at=%s WHERE id=%s", ('verified' if verified else 'unverified', availability, 'a'*64, '2026-11-03T10:00:00Z' if verified else None, version_id))
        if local and archive:
            self.catalog.set_local_fingerprint(vault_id=9, path=path, plaintext_sha256=('a' if match else 'b')*64, matched_archive_version_id=version_id if match else None)
        return file_id, version_id

    def test_every_state_browse_search_folder_rebuild_and_stats_agree(self):
        self.seed('pending')
        self.seed('protected', archive=True, verified=True)
        self.seed('changed', archive=True, verified=True, match=False)
        self.seed('missing', local=False, archive=True, verified=True, availability='missing')
        self.seed('unknown', archive=True, verified=True, availability='unknown')
        self.seed('cloud', local=False, archive=True, verified=True)
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        expected = {'pending': 2, 'protected': 1, 'problem': 1, 'unconfirmed': 1, 'cloud_only': 1}
        rows = self.catalog.list_file_rows(9)
        self.assertEqual(dict(Counter(row['state'] for row in rows)), expected)
        self.assertEqual(self.catalog.summary(9)['states'], expected)
        folder = self.catalog.list_files_page(9)['items'][0]
        self.assertEqual(folder['state_counts'], expected)
        self.assertEqual(folder['state'], 'problem')
        for state, count in expected.items():
            page = self.catalog.list_files_page(9, directory='photos', state=state)
            search = self.catalog.list_files_page(9, search='.jpg', state=state)
            self.assertEqual(page['total'], count)
            self.assertEqual(search['total'], count)
            self.assertTrue(all(item['state'] == state for item in page['items']))
        changed = next(item for item in rows if item['path'].endswith('/changed.jpg'))
        self.assertTrue(changed['previous_snapshot_protected'])
        self.assertFalse(changed['cleanup_eligible'])
        # Dirty SQL rollup and streamed full rebuild are identical.
        aggregates.mark_directory_dirty(self.connection, 9, 'photos')
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], expected)

    def test_absent_volume_api_is_unconfirmed_without_catalog_or_cloud_mutation(self):
        _, version = self.seed('protected', archive=True, verified=True)
        self.seed('cloud', local=False, archive=True, verified=True)
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        self.connection.commit()
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(False, True, 'photos', 'absent')):
            result = list_files(directory='photos', page=1, page_size=100, vault={'id': 9, 'source_root': '/source'})
            summary = stats({'id': 9, 'source_root': '/source'})
        self.assertEqual(summary['states'], {'unconfirmed': 2})
        self.assertEqual(summary['last_known_states'], {'protected': 1, 'cloud_only': 1})
        self.assertTrue(all(item['state'] == 'unconfirmed' for item in result['items']))
        self.assertTrue(all(not item['cleanup_eligible'] for item in result['items']))
        self.assertEqual(self.connection.execute('SELECT availability FROM archive_versions WHERE id=%s', (version,)).fetchone()['availability'], 'available')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) AS n FROM archive_versions').fetchone()['n'], 2)
        self.assertEqual(self.connection.execute("SELECT presence FROM local_copies WHERE presence='present'").fetchone()['presence'], 'present')

    def test_suspension_is_separate_from_verified_content(self):
        from app.services.operation_policies import get_policy, set_policy
        self.seed('protected', archive=True, verified=True)
        set_policy(self.connection, 9, replace(get_policy(self.connection, 9), auto_upload=False))
        self.connection.commit()
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(True, True, None, 'ok')):
            summary = stats({'id': 9, 'source_root': '/source'})
        self.assertTrue(summary['protection_suspended'])
        self.assertEqual(summary['states'], {'protected': 1})

    def test_failed_upload_mismatch_conflict_and_unverified_are_not_protected(self):
        failed_id, _ = self.seed('failed')
        self.connection.execute("INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_at, updated_at) VALUES (9, %s, 'photos/failed.jpg', 'upload', 'failed', '2026-11-03', '2026-11-03')", (failed_id,))
        conflict_id, version = self.seed('conflict', archive=True, verified=True, match=False)
        self.connection.execute('UPDATE local_copies SET cloud_only_version_id=%s WHERE vault_file_id=%s', (version, conflict_id))
        _, mismatch = self.seed('mismatch', archive=True)
        self.connection.execute("UPDATE archive_versions SET integrity='mismatch' WHERE id=%s", (mismatch,))
        self.seed('unverified', archive=True)
        self.seed('missing-local', archive=True, verified=True, availability='missing')
        states = {row['path']: row for row in self.catalog.list_file_rows(9)}
        for name in ('failed', 'conflict', 'mismatch', 'missing-local'):
            self.assertEqual(states[f'photos/{name}.jpg']['state'], 'problem')
            self.assertFalse(states[f'photos/{name}.jpg']['cleanup_eligible'])
        self.assertEqual(states['photos/unverified.jpg']['state'], 'pending')
        self.assertFalse(states['photos/conflict.jpg']['upload_eligible'])
        # Derivation cannot purge or supersede the only cloud snapshot.
        self.assertEqual(self.connection.execute('SELECT availability FROM archive_versions WHERE id=%s', (version,)).fetchone()['availability'], 'available')
