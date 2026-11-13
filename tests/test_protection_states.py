"""Protection observations; PostgreSQL parity runs only in PR CI."""
from __future__ import annotations

import json
import tempfile
import uuid
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import settings
from app.main import list_files, stats
from app.services import directory_aggregates as aggregates
from app.services.protection_states import (
    STATES, aggregate_state, observation_is_unconfirmed, project_file, state_sql,
)
from app.services.upload_path_rules import inline_eligibility_sql
from app.services.source_layout import VaultLocalAccess
from tests.catalog_support import fingerprint, protect, seed_vault

T = '2026-11-03T10:00:00+00:00'


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
        result = project_file({'protection_state': 'pending', 'current_version_id': 'v1',
                               'integrity': 'verified', 'availability': 'available'})
        self.assertTrue(result['previous_snapshot_protected'])

    def test_pending_upload_keeps_indicator_for_an_older_verified_snapshot(self):
        # The replacement in progress is never recoverable; Current still is.
        result = project_file({'protection_state': 'pending', 'current_version_id': 'v1',
                               'replacing_version_id': 'v2', 'integrity': 'verified',
                               'availability': 'available'})
        self.assertTrue(result['previous_snapshot_protected'])
        # Without an explicit Current nothing older is offered as protected.
        self.assertFalse(project_file({'protection_state': 'pending', 'replacing_version_id': 'v2',
                                       'integrity': 'verified', 'availability': 'available'})['previous_snapshot_protected'])

    def test_unknown_cloud_keeps_last_verified_state(self):
        result = project_file({'protection_state': 'unconfirmed', 'integrity': 'verified',
            'local_presence': 'present', 'matched_version_id': 'v1',
            'current_version_id': 'v1', 'local_sha256': 'a', 'version_sha256': 'a'})
        self.assertEqual(result['last_known_state'], 'protected')

    def test_aggregate_is_actionable_and_empty_is_not_protected(self):
        self.assertIsNone(aggregate_state({}))
        self.assertEqual(aggregate_state({'protected': 100, 'pending': 1}), 'pending')
        self.assertEqual(aggregate_state({'problem': 1, 'unconfirmed': 100}), 'problem')
        self.assertEqual(aggregate_state({'cloud_only': 2}), 'cloud_only')

    def test_excluded_never_outranks_actionable_states_and_is_reported_alone(self):
        self.assertEqual(aggregate_state({'excluded': 18, 'protected': 3}), 'protected')
        self.assertEqual(aggregate_state({'excluded': 18, 'cloud_only': 1}), 'cloud_only')
        self.assertEqual(aggregate_state({'excluded': 18, 'pending': 1}), 'pending')
        self.assertEqual(aggregate_state({'excluded': 18, 'problem': 1}), 'problem')
        self.assertEqual(aggregate_state({'excluded': 18}), 'excluded')
        self.assertIn('excluded', STATES)

    def test_stopped_ranks_below_pending_and_above_protected(self):
        # #630: a Stopped Upload is no queue, but a Vault holding one is not protected.
        self.assertIn('stopped', STATES)
        self.assertEqual(aggregate_state({'stopped': 5, 'protected': 100}), 'stopped')
        self.assertEqual(aggregate_state({'stopped': 5, 'pending': 1}), 'pending')
        self.assertEqual(aggregate_state({'stopped': 5, 'unconfirmed': 1}), 'unconfirmed')
        self.assertEqual(aggregate_state({'stopped': 5, 'problem': 1}), 'problem')
        self.assertEqual(aggregate_state({'stopped': 5, 'cloud_only': 3, 'excluded': 2}), 'stopped')

    def test_stopped_keeps_its_last_known_state_and_previous_cloud_copy(self):
        unknown = {'protection_state': 'unconfirmed', 'integrity': 'verified', 'local_presence': 'present',
                   'matched_version_id': None, 'current_version_id': 'v1',
                   'local_sha256': 'b', 'version_sha256': 'a'}
        self.assertEqual(project_file({**unknown, 'upload_stopped': True})['last_known_state'], 'stopped')
        self.assertEqual(project_file({**unknown, 'upload_stopped': True, 'path_excluded': True})['last_known_state'], 'excluded')
        result = project_file({'protection_state': 'stopped', 'current_version_id': 'v1',
                               'integrity': 'verified', 'availability': 'available'})
        self.assertEqual(result['state'], 'stopped')
        self.assertTrue(result['previous_snapshot_protected'])

    def test_unsupported_never_outranks_actionable_states_and_is_reported_alone(self):
        # #524: a symlink or FIFO is not a protection failure.
        self.assertEqual(aggregate_state({'unsupported': 4, 'protected': 3}), 'protected')
        self.assertEqual(aggregate_state({'unsupported': 4, 'pending': 1}), 'pending')
        self.assertEqual(aggregate_state({'unsupported': 4, 'problem': 1}), 'problem')
        self.assertEqual(aggregate_state({'unsupported': 4, 'excluded': 2}), 'unsupported')
        self.assertEqual(aggregate_state({'unsupported': 4}), 'unsupported')
        self.assertIn('unsupported', STATES)

    def test_unsupported_keeps_last_known_state_and_previous_cloud_copy(self):
        result = project_file({'protection_state': 'unconfirmed', 'integrity': 'verified', 'local_presence': 'unsupported'})
        self.assertEqual(result['last_known_state'], 'unsupported')
        result = project_file({'protection_state': 'unsupported', 'current_version_id': 'v1',
                               'integrity': 'verified', 'availability': 'available'})
        self.assertTrue(result['previous_snapshot_protected'])

    def test_state_sql_embeds_fixed_and_vault_rules_as_safe_literals(self):
        from app.services.operation_policies import OperationPolicy
        fixed = state_sql()
        self.assertIn("THEN 'excluded'", fixed)
        self.assertNotIn('%s', fixed)
        self.assertIn(inline_eligibility_sql(None), fixed)
        for pattern in ('._', 'DS_Store', 'Thumbs'):
            self.assertIn(pattern, fixed)
        policy = OperationPolicy(include_globs=("it's/**",), exclude_globs=("100%*.raw",))
        sql = state_sql(policy)
        # Driver placeholders stay exact: no stray %s from a pattern, % doubled.
        self.assertNotIn('%s', sql)
        self.assertIn('100%%', sql)
        self.assertIn("it''s", sql)

    def test_excluded_keeps_the_previous_cloud_copy_indication(self):
        result = project_file({'protection_state': 'excluded', 'current_version_id': 'v1',
                               'integrity': 'verified', 'availability': 'available'})
        self.assertTrue(result['previous_snapshot_protected'])
        self.assertEqual(result['state'], 'excluded')
        self.assertFalse(project_file({'protection_state': 'excluded'})['previous_snapshot_protected'])
        unknown = {'protection_state': 'unconfirmed', 'integrity': 'verified', 'local_presence': 'present',
                   'matched_version_id': None, 'current_version_id': 'v1',
                   'local_sha256': 'b', 'version_sha256': 'a'}
        self.assertEqual(project_file({**unknown, 'path_excluded': True})['last_known_state'], 'excluded')
        self.assertEqual(project_file(unknown)['last_known_state'], 'pending')

    def test_only_failed_observation_requests_make_protection_unknown(self):
        self.assertFalse(observation_is_unconfirmed({'audit': {'healthy': False}}))
        for component in ('source', 'cloud', 'audit'):
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
        seed_vault(self.connection, vault_id=9, name='Files')
        self.catalog = ArchiveCatalog(self.connection)
        self.cfg = replace(settings, database_dsn=database_dsn(str(self.path)))
        aggregates.set_maintenance_scheduling_enabled(False)
        self.addCleanup(aggregates.set_maintenance_scheduling_enabled, True)

    def seed(self, name, *, local=True, archive=False, verified=False, availability='available', match=True, path=None, file_type='regular'):
        """One Vault File; ``archive`` uploads a replacement, ``verified`` makes it Current."""
        path = path or f'photos/{name}.jpg'
        file_id = self.catalog.observe_local_copy(vault_id=9, path=path, file_type=file_type, size=12, mtime_ns=1, observed_at=T)
        version_id = None
        if archive:
            if file_type == 'regular':
                fingerprint(self.catalog, 9, path, ('a' if match else 'b') * 64)
            version_id = self.catalog.begin_replacement(
                file_id, object_key=f'vaults/x/{path}', provider_version_id=name, size=12,
                storage_class='STANDARD', created_at=T)
            if verified:
                self.catalog.promote_replacement(
                    file_id, archive_version_id=version_id, plaintext_sha256='a' * 64, verified_at=T,
                    local_generation=self.catalog.get_file_by_path(9, path)['generation'])
                if availability != 'available':
                    self.connection.execute('UPDATE archive_versions SET availability=%s WHERE id=%s', (availability, version_id))
        if not local:
            self.catalog.mark_local_copy_missing(file_id, observed_at=T)
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

    def test_default_excluded_files_have_their_own_state_and_do_not_block_protection(self):
        # #498: macOS metadata is skipped by automatic upload, so it is neither
        # pending work nor a reason for the Vault to stay unprotected.
        self.seed('protected', archive=True, verified=True)
        for name in ('._photo.jpg', '.DS_Store', 'sub/._deep'):
            self.seed(name, path=f'photos/{name}')
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        self.connection.commit()
        expected = {'protected': 1, 'excluded': 3}
        summary = self.catalog.summary(9)
        self.assertEqual(summary['states'], expected)
        self.assertEqual(summary['protection_state'], 'protected')
        folder = self.catalog.list_files_page(9)['items'][0]
        self.assertEqual(folder['state_counts'], expected)
        self.assertEqual(folder['state'], 'protected')
        self.assertEqual(self.catalog.list_files_page(9, directory='photos', state='pending')['total'], 0)
        # Two excluded files sit directly in photos/, the third in its sub-folder.
        page = self.catalog.list_files_page(9, directory='photos', state='excluded')
        self.assertEqual([item['type'] for item in page['items']], ['directory', 'file', 'file'])
        self.assertTrue(all(item['state'] == 'excluded' for item in page['items'] if item['type'] == 'file'))
        page = self.catalog.list_files_page(9, directory='photos', state='protected')
        self.assertEqual(page['total'], 1)
        self.assertEqual(self.catalog.list_files_page(9, search='ds_store', state='excluded')['total'], 1)
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(True, True, None, 'ok')):
            payload = stats({'id': 9, 'source_root': '/source'})
        self.assertEqual(payload['states'], expected)
        self.assertEqual(payload['protection_state'], 'protected')
        # Rollups and file queries agree, and a real pending file still counts.
        self.seed('waiting')
        aggregates.mark_directory_dirty(self.connection, 9, 'photos')
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.summary(9)['protection_state'], 'pending')
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], {**expected, 'pending': 1})

    def test_unsupported_entries_have_their_own_state_and_do_not_make_the_vault_a_problem(self):
        # #524: symlinks are never followed and FIFOs cannot be archived; the
        # Vault stays protected and the entries are listed under their own filter.
        self.seed('protected', archive=True, verified=True)
        self.seed('link', path='photos/link', file_type='symlink')
        self.seed('pipe', path='photos/sub/pipe', file_type='other')
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        self.connection.commit()
        expected = {'protected': 1, 'unsupported': 2}
        summary = self.catalog.summary(9)
        self.assertEqual(summary['states'], expected)
        self.assertEqual(summary['protection_state'], 'protected')
        folder = self.catalog.list_files_page(9)['items'][0]
        self.assertEqual(folder['state_counts'], expected)
        self.assertEqual(folder['state'], 'protected')
        self.assertEqual(self.catalog.list_files_page(9, directory='photos', state='problem')['total'], 0)
        page = self.catalog.list_files_page(9, directory='photos', state='unsupported')
        self.assertEqual([item['type'] for item in page['items']], ['directory', 'file'])
        self.assertEqual(page['items'][1]['state'], 'unsupported')
        self.assertFalse(page['items'][1]['upload_eligible'])
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(True, True, None, 'ok')):
            payload = stats({'id': 9, 'source_root': '/source'})
        self.assertEqual(payload['states'], expected)
        self.assertEqual(payload['protection_state'], 'protected')
        # The dirty SQL rollup agrees with the streamed rebuild.
        aggregates.mark_directory_dirty(self.connection, 9, 'photos')
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], expected)

    def test_only_excluded_files_are_reported_excluded_and_a_manual_upload_protects_them(self):
        file_id, _ = self.seed('._only', path='._only')
        self.assertEqual(self.catalog.summary(9)['states'], {'excluded': 1})
        self.assertEqual(self.catalog.summary(9)['protection_state'], 'excluded')
        row = self.catalog.list_file_rows(9)[0]
        self.assertEqual(row['state'], 'excluded')
        # Manual operations stay explicit: the file remains uploadable...
        self.assertTrue(row['upload_eligible'])
        # ...and once verified in the cloud it is Protected like any other file.
        fingerprint(self.catalog, 9, '._only', 'a' * 64)
        protect(self.catalog, 9, '._only', 'a' * 64)
        self.assertEqual(self.catalog.summary(9)['states'], {'protected': 1})

    def test_vault_exclusion_globs_define_excluded_and_removing_them_restores_pending(self):
        from app.services.operation_policies import OperationPolicy, set_policy
        self.seed('keep')
        self.seed('raw', path='photos/shot.raw')
        self.assertEqual(self.catalog.summary(9)['states'], {'pending': 2})
        set_policy(self.connection, 9, OperationPolicy(exclude_globs=('*.raw',)))
        self.assertEqual(self.catalog.summary(9)['states'], {'pending': 1, 'excluded': 1})
        self.assertEqual(self.catalog.list_files_page(9, directory='photos', state='excluded')['items'][0]['path'], 'photos/shot.raw')
        set_policy(self.connection, 9, OperationPolicy(include_globs=('keep*',)))
        self.assertEqual(self.catalog.summary(9)['states'], {'pending': 1, 'excluded': 1})
        set_policy(self.connection, 9, OperationPolicy())
        self.assertEqual(self.catalog.summary(9)['states'], {'pending': 2})

    def test_excluded_does_not_mask_problems_and_keeps_the_previous_cloud_copy_visible(self):
        failed_id, _ = self.seed('._failed', path='photos/._failed')
        self.connection.execute("INSERT INTO jobs(vault_id, file_id, action, status) VALUES (9, %s, 'upload', 'failed')", (failed_id,))
        conflict_id, version = self.seed('._held', path='photos/._held', archive=True, verified=True, match=False)
        self.connection.execute('UPDATE files SET held_version_id=%s WHERE id=%s', (version, conflict_id))
        # Changed after a verified upload, then excluded: the current bytes are not
        # in the cloud (Excluded is not "safe"), but the older copy stays indicated.
        self.seed('._stale', path='photos/._stale', archive=True, verified=True, match=False)
        # Availability unknown wins over the rule, yet the last known state is Excluded.
        self.seed('._unknown', path='photos/._unknown', archive=True, verified=True, availability='unknown', match=False)
        self.assertEqual(self.catalog.summary(9)['states'], {'problem': 2, 'excluded': 1, 'unconfirmed': 1})
        rows = {row['path']: row for row in self.catalog.list_file_rows(9)}
        stale = rows['photos/._stale']
        self.assertEqual(stale['state'], 'excluded')
        self.assertTrue(stale['previous_snapshot_protected'])
        self.assertFalse(stale['cleanup_eligible'])
        unknown = rows['photos/._unknown']
        self.assertEqual((unknown['state'], unknown['last_known_state']), ('unconfirmed', 'excluded'))
        self.assertEqual(rows['photos/._failed']['state'], 'problem')

    def job(self, file_id, status, *, origin='manual', key=None, at='2026-11-03T10:05:00+00:00'):
        """An upload Job of ``file_id`` requested at ``at`` (after the seed's observation)."""
        finished = at if status in ('completed', 'failed', 'cancelled') else None
        return self.connection.execute(
            "INSERT INTO jobs(vault_id, file_id, action, status, origin, message_key, requested_at, updated_at, finished_at) "
            "VALUES (9, %s, 'upload', %s, %s, %s, %s, %s, %s) RETURNING id",
            (file_id, status, origin, key, at, at, finished)).fetchone()['id']

    def test_owner_stopped_upload_is_its_own_state_until_the_content_changes(self):
        # #630: the Owner cancelled the upload; admission holds the File, so it
        # is shown as stopped, not pending, everywhere the state is read.
        self.seed('protected', archive=True, verified=True)
        stopped_id, _ = self.seed('stopped', path='photos/sub/stopped.jpg')
        self.job(stopped_id, 'cancelled', key='job.upload_stopped')
        auto_id, _ = self.seed('auto', path='photos/sub/auto.jpg')
        self.job(auto_id, 'cancelled', origin='automatic', key='job.upload_stopped')
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        self.connection.commit()
        expected = {'protected': 1, 'stopped': 2}
        summary = self.catalog.summary(9)
        self.assertEqual(summary['states'], expected)
        self.assertEqual(summary['protection_state'], 'stopped')
        rows = {row['path']: row for row in self.catalog.list_file_rows(9)}
        stopped = rows['photos/sub/stopped.jpg']
        self.assertEqual((stopped['state'], stopped['last_known_state']), ('stopped', 'stopped'))
        # A manual upload stays possible.
        self.assertTrue(stopped['upload_eligible'])
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(True, True, None, 'ok')):
            payload = stats({'id': 9, 'source_root': '/source'})
        self.assertEqual(payload['states'], expected)
        self.assertEqual(payload['protection_state'], 'stopped')
        # Folders: the projection holds them as pending; the listing reads them live.
        for directory, name in (('', 'photos'), ('photos', 'photos/sub')):
            with self.subTest(directory=directory):
                folder = next(item for item in self.catalog.list_files_page(9, directory=directory)['items']
                              if item['path'] == name)
                self.assertEqual(folder['state_counts'], expected if name == 'photos' else {'stopped': 2})
                self.assertEqual(folder['state'], 'stopped')
                stopped_page = self.catalog.list_files_page(9, directory=directory, state='stopped')
                self.assertEqual([item['path'] for item in stopped_page['items']], [name])
                self.assertEqual(self.catalog.list_files_page(9, directory=directory, state='pending')['total'], 0)
        page = self.catalog.list_files_page(9, directory='photos/sub', state='stopped')
        self.assertEqual(page['total'], 2)
        self.assertEqual(self.catalog.list_files_page(9, search='.jpg', state='stopped')['total'], 2)
        # The dirty SQL rollup agrees with the streamed rebuild.
        aggregates.mark_directory_dirty(self.connection, 9, 'photos/sub')
        aggregates.mark_directory_dirty(self.connection, 9, 'photos')
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], expected)

        # A manual upload the Owner asks for runs: the File is pending again.
        manual = self.job(stopped_id, 'queued', at='2026-11-03T10:06:00+00:00')
        self.assertEqual(self.catalog.summary(9)['states'], {'protected': 1, 'stopped': 1, 'pending': 1})
        folder = self.catalog.list_files_page(9)['items'][0]
        self.assertEqual(folder['state_counts'], {'protected': 1, 'stopped': 1, 'pending': 1})
        self.assertEqual(folder['state'], 'pending')
        self.assertEqual(self.catalog.list_files_page(9, state='pending')['total'], 1)
        self.connection.execute("UPDATE jobs SET status='cancelled', message_key='job.upload_stopped', "
                                "finished_at=requested_at WHERE id=%s", (manual,))
        # Changed content is observed after the stop: automatic upload resumes.
        self.catalog.observe_local_copy(vault_id=9, path='photos/sub/stopped.jpg', file_type='regular',
                                        size=13, mtime_ns=2, observed_at='2026-11-03T10:10:00+00:00')
        self.assertEqual(self.catalog.summary(9)['states'], {'protected': 1, 'stopped': 1, 'pending': 1})
        states = {row['path']: row['state'] for row in self.catalog.list_file_rows(9)}
        self.assertEqual(states['photos/sub/stopped.jpg'], 'pending')
        self.assertEqual(states['photos/sub/auto.jpg'], 'stopped')

    def test_a_failed_upload_stays_a_problem_and_other_closures_do_not_stop(self):
        failed_auto, _ = self.seed('failed_auto')
        self.job(failed_auto, 'failed', origin='automatic')
        failed_manual, _ = self.seed('failed_manual')
        self.job(failed_manual, 'failed')
        # A manual upload closed for another reason (removed file) holds nothing.
        removed, _ = self.seed('removed')
        self.job(removed, 'cancelled', key='job.upload_file_removed')
        # A stop older than the content observation no longer holds.
        old, _ = self.seed('old')
        self.job(old, 'cancelled', key='job.upload_stopped', at='2026-11-03T09:00:00+00:00')
        # An excluded path stays excluded even when its upload was stopped.
        noise, _ = self.seed('._noise', path='photos/._noise')
        self.job(noise, 'cancelled', key='job.upload_stopped')
        states = {row['path']: row['state'] for row in self.catalog.list_file_rows(9)}
        self.assertEqual(states, {
            'photos/failed_auto.jpg': 'problem', 'photos/failed_manual.jpg': 'problem',
            'photos/removed.jpg': 'pending', 'photos/old.jpg': 'pending', 'photos/._noise': 'excluded',
        })
        self.assertEqual(self.catalog.summary(9)['states'], {'problem': 2, 'pending': 2, 'excluded': 1})

    def test_a_stored_unsupported_glob_cannot_break_classification_or_rebuilds(self):
        # Saved before the glob was validated (or by hand): it must never reach
        # PostgreSQL as an invalid regular expression (#498 review).
        self.seed('keep')
        self.seed('._meta', path='photos/._meta')
        self.connection.execute(
            "INSERT INTO vault_operation_policies(vault_id, auto_upload, stability_seconds, include_globs_json, exclude_globs_json) "
            "VALUES (9, TRUE, 300, '[]', %s)", (json.dumps(["[z-a]*", "a\u0000b", "[a-z[:digit:]]*", "*a" * 6500 + "*"]),))
        self.assertEqual(self.catalog.summary(9)['states'], {'pending': 1, 'excluded': 1})
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        aggregates.mark_directory_dirty(self.connection, 9, 'photos')
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], {'pending': 1, 'excluded': 1})
        self.assertEqual(self.catalog.list_files_page(9, directory='photos', state='excluded')['total'], 1)

    def test_global_maintenance_skips_a_failing_vault_and_keeps_its_marks(self):
        seed_vault(self.connection, vault_id=10, source_root='/other', name='Other')
        for vault_id in (9, 10):
            aggregates.request_vault_rebuild(self.connection, vault_id)
        real = aggregates.flush_directory_aggregates

        def flaky(connection, *, vault_id):
            if vault_id == 9:
                connection.execute("SELECT 'x' ~ '[[:alpha:'")  # a genuine PostgreSQL regex error
            return real(connection, vault_id=vault_id)

        with patch.object(aggregates, 'flush_directory_aggregates', side_effect=flaky):
            totals = aggregates.process_directory_aggregate_maintenance(connection=self.connection, publish=False)
        self.assertEqual((totals['skipped_vaults'], totals['vaults']), (1, 1))
        status = {row['vault_id']: row['status'] for row in self.connection.execute('SELECT vault_id, status FROM directory_aggregate_status').fetchall()}
        self.assertEqual(status, {9: 'rebuild_required', 10: 'ready'})

    def test_absent_volume_api_is_unconfirmed_without_catalog_or_cloud_mutation(self):
        _, version = self.seed('protected', archive=True, verified=True)
        self.seed('cloud', local=False, archive=True, verified=True)
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        self.connection.commit()
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(False, True, 'photos', 'absent')):
            result = list_files(directory='photos', page=1, page_size=100, vault={'id': 9, 'source_root': '/source'})
            summary = stats({'id': 9, 'source_root': '/source'})
            root = list_files(directory='', state='unconfirmed', page=1, page_size=100, vault={'id': 9, 'source_root': '/source'})
            no_protected = list_files(directory='', state='protected', page=1, page_size=100, vault={'id': 9, 'source_root': '/source'})
        self.assertEqual(root['items'][0]['state_counts'], {'unconfirmed': 2})
        self.assertEqual(root['items'][0]['last_known_states'], {'protected': 1, 'cloud_only': 1})
        self.assertEqual(root['items'][0]['available_actions']['free-space'], 0)
        self.assertEqual(no_protected['total'], 0)
        self.assertEqual(summary['states'], {'unconfirmed': 2})
        self.assertEqual(summary['last_known_states'], {'protected': 1, 'cloud_only': 1})
        self.assertTrue(all(item['state'] == 'unconfirmed' for item in result['items']))
        self.assertTrue(all(not item['cleanup_eligible'] for item in result['items']))
        self.assertEqual(self.connection.execute('SELECT availability FROM archive_versions WHERE id=%s', (version,)).fetchone()['availability'], 'available')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) AS n FROM archive_versions').fetchone()['n'], 2)
        self.assertEqual(self.connection.execute("SELECT local_presence FROM files WHERE local_presence='present'").fetchone()['local_presence'], 'present')

    def test_suspension_is_separate_from_verified_content(self):
        from app.services.operation_policies import get_policy, set_policy
        self.seed('protected', archive=True, verified=True)
        set_policy(self.connection, 9, replace(get_policy(self.connection, 9), auto_upload=False))
        self.connection.commit()
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(True, True, None, 'ok')):
            summary = stats({'id': 9, 'source_root': '/source'})
        self.assertTrue(summary['protection_suspended'])
        self.assertEqual(summary['states'], {'protected': 1})

    def test_cloud_request_failure_is_unconfirmed_but_known_missing_is_problem(self):
        from app.services.component_health import record_component_health
        self.seed('protected', archive=True, verified=True)
        self.seed('missing', local=False, archive=True, verified=True, availability='missing')
        record_component_health(self.connection, vault_id=9, component='cloud', ok=False, error='offline')
        self.connection.commit()
        with patch('app.database.settings', self.cfg), patch('app.main.vault_local_access', return_value=VaultLocalAccess(True, True, None, 'ok')):
            self.assertEqual(stats({'id': 9, 'source_root': '/source'})['states'], {'unconfirmed': 2})
            record_component_health(self.connection, vault_id=9, component='cloud', ok=True)
            self.connection.commit()
            self.assertEqual(stats({'id': 9, 'source_root': '/source'})['states'], {'protected': 1, 'problem': 1})

    def test_terminal_upload_failure_invalidates_folder_and_retry_clears_it(self):
        from app.storage import set_job
        file_id, _ = self.seed('failed')
        job = self.connection.execute("INSERT INTO jobs(vault_id, file_id, action, status) VALUES (9, %s, 'upload', 'queued') RETURNING id", (file_id,)).fetchone()['id']
        aggregates.rebuild_vault_directory_aggregates(self.connection, 9)
        self.connection.commit()
        with patch('app.database.settings', self.cfg):
            self.assertTrue(set_job(job, 'failed'))
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], {'problem': 1})
        self.catalog.queue_jobs(vault_id=9, path='photos/failed.jpg', action='upload', requested_at=T, group_id=str(uuid.uuid4()), is_directory=False)
        aggregates.flush_directory_aggregates(self.connection, vault_id=9)
        self.assertEqual(self.catalog.list_files_page(9)['items'][0]['state_counts'], {'pending': 1})

    def test_failed_upload_mismatch_conflict_and_unverified_are_not_protected(self):
        failed_id, _ = self.seed('failed')
        self.connection.execute("INSERT INTO jobs(vault_id, file_id, action, status) VALUES (9, %s, 'upload', 'failed')", (failed_id,))
        conflict_id, version = self.seed('conflict', archive=True, verified=True, match=False)
        self.connection.execute('UPDATE files SET held_version_id=%s WHERE id=%s', (version, conflict_id))
        # A reconciliation block replaces the v0.5 mismatch state (#587).
        blocked_id, _ = self.seed('mismatch', archive=True)
        self.catalog.block_reconciliation(blocked_id, reason='proof_mismatch', at=T)
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
