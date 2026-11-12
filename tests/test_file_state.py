"""Real PG16 contract for the new native current-file baseline component.

Runtime cutover is pending. Apply the canonical component DDL to an isolated
migrated fixture; never use compatibility views, mirrored legacy rows or a fake DB.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import UUID, uuid4

import psycopg

from app.file_state import FileStateConflict, FileStateStore
from tests.postgres import PostgresConnection, run_alembic

AT = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
LATER = AT + timedelta(seconds=3)
MTIME = 1_597_614_120_123_456_789
SHA = hashlib.sha256(b'file state').digest()
BASELINE = Path(__file__).resolve().parents[1] / 'migrations/baseline'


class FileStateFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = Path(directory.name) / 'native-file-state'
        result = run_alembic(self.identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.db() as connection:
            connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
                VALUES (2,'native','Native','/unused-native','bucket','native'),
                       (3,'other','Other','/unused-other','bucket','other')""")
            connection.execute((BASELINE / 'file_state.sql').read_text())
            connection.execute((BASELINE / 'upload_evidence.sql').read_text())

    @contextmanager
    def db(self):
        # Also close when a deferred constraint rejects COMMIT itself.
        with closing(PostgresConnection(self.identity)) as connection:
            with connection:
                yield connection

    def observe(self, store, **changes):
        args = dict(vault_id=2, path='a/file.txt', file_type='regular', size=10, mtime_ns=MTIME, at=AT)
        args.update(changes)
        return store.observe(**args)


class FileStateTests(FileStateFixture):
    def test_identity_current_path_and_observation_are_one_native_row(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            identity = store.create_identity(vault_id=2, path='a/file.txt', at=AT)
            self.assertIsNone(identity['observation_generation'])
            row = self.observe(store)
            self.assertEqual(row['id'], identity['id'])
            self.assertIsInstance(row['id'], UUID)
            self.assertIsInstance(row['path_generation'], UUID)
            self.assertIsInstance(row['observation_generation'], UUID)
            self.assertEqual(row['path_since'], AT)
            self.assertEqual(row['local_observed_at'], AT)
            self.assertEqual(row['local_mtime_ns'], MTIME)
            self.assertEqual(store.history(vault_id=2, file_id=row['id']), [])
            # This implementation neither creates nor mirrors legacy rows.
            for table in ('vault_files', 'file_paths', 'local_copies'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) n FROM {table}').fetchone()['n'], 0)

    def test_reobservation_preserves_identity_path_and_unchanged_fingerprint(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            first = self.observe(store)
            hashed = store.publish_digest(vault_id=2, file_id=first['id'],
                path_generation=first['path_generation'], observation_generation=first['observation_generation'], digest=SHA)
            again = self.observe(store, at=LATER)
            self.assertEqual(again['id'], first['id'])
            self.assertEqual(again['path_generation'], first['path_generation'])
            self.assertNotEqual(again['observation_generation'], hashed['observation_generation'])
            self.assertEqual(again['local_sha256'], SHA)
            self.assertEqual(again['local_content_observed_at'], AT)
            changed = self.observe(store, mtime_ns=MTIME + 1, at=LATER)
            self.assertIsNone(changed['local_sha256'])
            self.assertEqual(changed['local_content_observed_at'], LATER)

    def test_newer_observation_and_rename_each_fence_old_hash_publication(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            old = self.observe(store)
            current = self.observe(store, at=LATER)
            self.assertIsNone(store.publish_digest(vault_id=2, file_id=old['id'],
                path_generation=old['path_generation'], observation_generation=old['observation_generation'], digest=SHA))
            renamed = store.rename(vault_id=2, file_id=current['id'],
                path_generation=current['path_generation'], new_path='b/file.txt', at=LATER)
            self.assertIsNone(store.publish_digest(vault_id=2, file_id=current['id'],
                path_generation=current['path_generation'], observation_generation=current['observation_generation'], digest=SHA))
            hashed = store.publish_digest(vault_id=2, file_id=renamed['id'],
                path_generation=renamed['path_generation'], observation_generation=renamed['observation_generation'], digest=SHA)
            self.assertEqual(hashed['local_sha256'], SHA)
            self.assertIsNone(store.publish_digest(vault_id=2, file_id=renamed['id'],
                path_generation=renamed['path_generation'], observation_generation=renamed['observation_generation'], digest=SHA))

    def test_rename_closes_history_without_a_second_current_path(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            old = self.observe(store)
            moved = store.rename(vault_id=2, file_id=old['id'], path_generation=old['path_generation'], new_path='b.txt', at=LATER)
            self.assertEqual(moved['id'], old['id'])
            self.assertNotEqual(moved['path_generation'], old['path_generation'])
            self.assertEqual(moved['observation_generation'], old['observation_generation'])
            history = store.history(vault_id=2, file_id=old['id'])
            self.assertEqual(len(history), 1)
            self.assertEqual((history[0]['path'], history[0]['path_generation'], history[0]['valid_from'], history[0]['valid_to']),
                             ('a/file.txt', old['path_generation'], AT, LATER))
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM file_state').fetchone()['n'], 1)
            self.assertEqual(store.rename(vault_id=2, file_id=moved['id'], path_generation=moved['path_generation'], new_path='b.txt', at=LATER), moved)
            self.assertEqual(len(store.history(vault_id=2, file_id=old['id'])), 1)
            self.assertIsNone(store.get(vault_id=3, file_id=old['id']))
            self.assertEqual(store.history(vault_id=3, file_id=old['id']), [])

    def test_stale_rename_can_be_caught_without_committing_partial_history(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            row = self.observe(store)
            with self.assertRaises(FileStateConflict):
                store.rename(vault_id=2, file_id=row['id'], path_generation=uuid4(), new_path='wrong.txt', at=LATER)
            store.create_identity(vault_id=2, path='other.txt', at=LATER)
        with self.db() as connection:
            store = FileStateStore(connection)
            self.assertEqual(store.get(vault_id=2, file_id=row['id']), row)
            self.assertEqual(store.history(vault_id=2, file_id=row['id']), [])

    def test_occupied_path_rolls_back_without_history_or_identity_loss(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            row = self.observe(store)
            other = store.create_identity(vault_id=2, path='taken.txt', at=AT)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.db() as connection:
                FileStateStore(connection).rename(vault_id=2, file_id=row['id'], path_generation=row['path_generation'], new_path='taken.txt', at=LATER)
        with self.db() as connection:
            store = FileStateStore(connection)
            self.assertEqual(store.get(vault_id=2, file_id=row['id']), row)
            self.assertEqual(store.get(vault_id=2, file_id=other['id']), other)
            self.assertEqual(store.history(vault_id=2, file_id=row['id']), [])

    def test_database_enforces_scopes_and_closed_history(self):
        with self.db() as connection:
            row = self.observe(FileStateStore(connection))
        for vault, closed in ((3, LATER), (2, None)):
            with self.subTest(vault=vault, closed=closed):
                with self.assertRaises(psycopg.IntegrityError):
                    with self.db() as connection:
                        connection.execute('''INSERT INTO file_path_history(vault_file_id,vault_id,path,path_generation,valid_from,valid_to)
                            VALUES (%s,%s,%s,%s,%s,%s)''', (row['id'],vault,'a/file.txt',row['path_generation'],AT,closed))

    def test_path_digest_and_timestamp_contracts_are_not_legacy_coercions(self):
        with self.db() as connection:
            store = FileStateStore(connection)
            for path in ('', '/abs.txt', 'a/../b', './a', 'a//b', 'a/'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    store.create_identity(vault_id=2, path=path, at=AT)
            with self.assertRaises(ValueError):
                store.create_identity(vault_id=2, path='x', at=AT.replace(tzinfo=None))
            row = self.observe(store, mtime_ns=-1)
            self.assertEqual(row['local_mtime_ns'], -1)
            with self.assertRaises(ValueError):
                store.publish_digest(vault_id=2, file_id=row['id'], path_generation=row['path_generation'],
                    observation_generation=row['observation_generation'], digest=b'bad')
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db() as connection:
                connection.execute("UPDATE file_state SET current_path='a/../b' WHERE id=%s", (row['id'],))

    def test_caller_rollback_owns_all_changes(self):
        with self.assertRaisesRegex(RuntimeError, 'abort caller'):
            with self.db() as connection:
                row = self.observe(FileStateStore(connection))
                FileStateStore(connection).rename(vault_id=2, file_id=row['id'], path_generation=row['path_generation'], new_path='moved', at=LATER)
                raise RuntimeError('abort caller')
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM file_state').fetchone()['n'], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM file_path_history').fetchone()['n'], 0)

    def test_concurrent_observers_create_one_identity_not_parallel_path_rows(self):
        barrier = threading.Barrier(2)

        def observe(index):
            with self.db() as connection:
                barrier.wait(timeout=10)
                return self.observe(FileStateStore(connection), at=AT + timedelta(seconds=index))

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(observe, (0, 1)))
        self.assertEqual(rows[0]['id'], rows[1]['id'])
        self.assertNotEqual(rows[0]['observation_generation'], rows[1]['observation_generation'])
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM file_state').fetchone()['n'], 1)
