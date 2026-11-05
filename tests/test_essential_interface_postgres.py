"""CI-only PostgreSQL global-search/sole-copy regressions (#430)."""
import unittest
from tests import test_protection_states as fixtures
from app.catalog import ArchiveCatalog


class EssentialSearchPostgresTests(unittest.TestCase):
    setUp = fixtures.ProtectionStatePostgresTests.setUp
    seed = fixtures.ProtectionStatePostgresTests.seed

    def test_duplicate_paths_keep_vault_identity_filter_and_pagination(self):
        _, first_version = self.seed('same', local=False, archive=True, verified=True)
        self.connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (10,'second','Second','/second','bucket','second')")
        second = self.catalog.record_archive_version(vault_id=10, path='photos/same.jpg', object_key='second/photos/same.jpg', provider_version_id='second-copy', size=12, storage_class='DEEP_ARCHIVE', etag='e', uploaded_at='2026-11-03T10:00:00Z', observed_at='2026-11-03T10:00:00Z', scan_id='scan')
        self.connection.execute("UPDATE archive_versions SET integrity='verified',availability='available',plaintext_sha256=%s WHERE id=%s", ('b'*64, second))
        vaults = [{'id':9,'name':'Files','unconfirmed':False}, {'id':10,'name':'Second','unconfirmed':False}]
        one = self.catalog.search_protected_vaults(vaults, search='photos/same', state='cloud_only', page=1, page_size=1)
        two = self.catalog.search_protected_vaults(vaults, search='photos/same', state='cloud_only', page=2, page_size=1)
        self.assertEqual(one['total'], 2)
        self.assertEqual([one['items'][0]['vault_id'],two['items'][0]['vault_id']], [9,10])
        self.assertEqual(two['items'][0]['vault_name'], 'Second')
        vaults[1]['unconfirmed'] = True
        missing = self.catalog.search_protected_vaults(vaults, search='same', state='cloud_only', page=1, page_size=100)
        self.assertEqual(missing['total'],1)
        self.assertEqual(missing['items'][0]['vault_id'],9)
        # Read-only search must never mutate or discard either sole cloud copy.
        for version in [first_version, second]:
            self.assertEqual(self.connection.execute('SELECT availability FROM archive_versions WHERE id=%s',(version,)).fetchone()['availability'],'available')
        self.assertEqual(self.connection.execute('SELECT count(*) AS n FROM archive_versions').fetchone()['n'],2)
