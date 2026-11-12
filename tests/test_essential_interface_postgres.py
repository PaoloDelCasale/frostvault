"""CI-only PostgreSQL global-search/sole-copy regressions (#430)."""
import unittest
from tests import test_protection_states as fixtures
from app.catalog import ArchiveCatalog


class EssentialSearchPostgresTests(unittest.TestCase):
    setUp = fixtures.ProtectionStatePostgresTests.setUp
    seed = fixtures.ProtectionStatePostgresTests.seed

    def test_recursive_estimate_includes_released_missing_copies_only_in_its_vault(self):
        from unittest.mock import patch
        from app.main import recover_estimate, RecoverEstimateRequest
        stamp = '2026-11-03T10:00:00Z'
        file_id, version = self.seed('cold', archive=True, verified=True)
        self.catalog.mark_local_copy_missing(file_id, observed_at=stamp)
        self.connection.execute("UPDATE archive_versions SET storage_class='DEEP_ARCHIVE' WHERE id=%s", (version,))
        self.connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (10,'second','Second','/second','bucket','second')")
        other = self.catalog.record_archive_version(vault_id=10, path='photos/cold.jpg', object_key='second/photos/cold.jpg', provider_version_id='other', size=12, storage_class='STANDARD', etag='e', uploaded_at=stamp, observed_at=stamp, scan_id='scan')
        self.catalog.mark_version_verified(other, plaintext_sha256='a'*64, verified_at=stamp)
        self.connection.commit()
        request = RecoverEstimateRequest(path='photos', is_directory=True, restore_tier='Bulk')
        with patch('app.database.settings', self.cfg), patch('app.main.settings', self.cfg):
            cold = recover_estimate(request, {'id':9})
            hot = recover_estimate(request, {'id':10})
        self.assertTrue(cold['requires_restore'])
        self.assertEqual(cold['estimate']['tier'], 'Bulk')
        self.assertIsInstance(cold['estimate']['estimated_hours'], (int, float))
        self.assertFalse(hot['requires_restore'])

    def test_http_config_scope_survives_another_clients_session_selection(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from app.main import app
        from app.sessions import create_session
        from app.services.operation_policies import get_policy
        self.connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (10,'second','Second','/second','bucket','second')")
        self.connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'test')")
        token = create_session(self.connection, user_id=1, auth_method='local')
        self.connection.execute('UPDATE sessions SET vault_id=9')
        self.connection.commit()
        with patch('app.main.settings', self.cfg), patch('app.database.settings', self.cfg), patch('app.sessions.settings', self.cfg):
            client = TestClient(app)
            client.cookies.set(self.cfg.session_cookie_name, token)
            csrf = client.get('/api/me').json()['csrf_token']
            # This tab displays 10 while another tab's session is selected on 9.
            result = client.put('/api/vault/operation-policy?vault_id=10', headers={'X-CSRF-Token':csrf}, json={'local_release_after_days':180})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(get_policy(self.connection,10).local_release_after_days,180)
            self.assertIsNone(get_policy(self.connection,9).local_release_after_days)
            self.assertEqual(client.get('/api/me').json()['vault']['id'],9)
            denied = client.put('/api/vault/operation-policy?vault_id=10',json={'local_release_after_days':None})
            self.assertEqual(denied.status_code,403)
            missing = client.get('/api/vault/operation-policy?vault_id=999')
            self.assertEqual(missing.status_code,404)
            self.assertEqual(client.get('/api/me').json()['vault']['id'],9)

    def test_duplicate_paths_keep_vault_identity_filter_and_pagination(self):
        _, first_version = self.seed('same', local=False, archive=True, verified=True)
        self.connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (10,'second','Second','/second','bucket','second')")
        second = self.catalog.record_archive_version(vault_id=10, path='photos/same.jpg', object_key='second/photos/same.jpg', provider_version_id='second-copy', size=12, storage_class='DEEP_ARCHIVE', etag='e', uploaded_at='2026-11-03T10:00:00Z', observed_at='2026-11-03T10:00:00Z', scan_id='scan')
        self.catalog.mark_version_verified(second, plaintext_sha256='b'*64, verified_at='2026-11-03T10:00:00Z')
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
