"""CI-only PostgreSQL global-search/sole-copy regressions (#430)."""
from tests import test_protection_states as fixtures
from tests.catalog_support import fingerprint
from tests import catalog_probe
from tests.fixtures import create_owner, seed_vault, sign_in


class EssentialSearchPostgresTests(fixtures.ProtectionStateFixture):
    def _cloud_only(self, vault_id, path, provider_version_id, storage_class, digest):
        """A verified Current whose Local Copy is gone, in ``vault_id``."""
        stamp = '2026-11-03T10:00:00Z'
        file_id = self.catalog.observe_local_copy(
            vault_id=vault_id, path=path, file_type='regular', size=12, mtime_ns=1, observed_at=stamp)
        fingerprint(self.catalog, vault_id, path, digest)
        version = self.catalog.begin_replacement(
            file_id, object_key=f'vaults/x/{path}', provider_version_id=provider_version_id,
            size=12, storage_class=storage_class, created_at=stamp)
        self.catalog.promote_replacement(
            file_id, archive_version_id=version, plaintext_sha256=digest, verified_at=stamp,
            local_generation=catalog_probe.get_file_by_path(self.catalog, vault_id, path)['generation'])
        self.catalog.mark_local_copy_missing(file_id, observed_at=stamp)
        return version

    def test_recursive_estimate_includes_released_missing_copies_only_in_its_vault(self):
        from app.services.job_admission import estimate_recovery
        stamp = '2026-11-03T10:00:00Z'
        file_id, version = self.seed('cold', archive=True, verified=True)
        self.catalog.mark_local_copy_missing(file_id, observed_at=stamp)
        self.connection.execute("UPDATE archive_versions SET storage_class='DEEP_ARCHIVE' WHERE id=%s", (version,))
        seed_vault(self.connection, id=10, name='Second', source_root='/second')
        self._cloud_only(10, 'photos/cold.jpg', 'other', 'STANDARD', 'a' * 64)
        self.connection.commit()
        request = dict(path='photos', is_directory=True, restore_tier='Bulk')
        cold = estimate_recovery(9, **request)
        hot = estimate_recovery(10, **request)
        self.assertTrue(cold['requires_restore'])
        self.assertEqual(cold['estimate']['tier'], 'Bulk')
        self.assertIsInstance(cold['estimate']['estimated_hours'], (int, float))
        self.assertFalse(hot['requires_restore'])

    def test_http_config_scope_survives_another_clients_session_selection(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services.operation_policies import get_policy
        seed_vault(self.connection, id=10, name='Second', source_root='/second')
        create_owner(self.connection)
        self.connection.commit()
        client = TestClient(app)
        sign_in(client, self.database, auth_method='local')
        self.connection.execute('UPDATE sessions SET vault_id=9')
        self.connection.commit()
        csrf = client.get('/api/me').json()['csrf_token']
        # This tab displays 10 while another tab's session is selected on 9.
        result = client.put('/api/vaults/10/operation-policy', headers={'X-CSRF-Token':csrf}, json={'local_release_after_days':180})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(get_policy(self.connection,10).local_release_after_days,180)
        self.assertIsNone(get_policy(self.connection,9).local_release_after_days)
        self.assertEqual(client.get('/api/me').json()['vault']['id'],9)
        denied = client.put('/api/vaults/10/operation-policy',json={'local_release_after_days':None})
        self.assertEqual(denied.status_code,403)
        missing = client.get('/api/vaults/999/operation-policy')
        self.assertEqual(missing.status_code,404)
        self.assertEqual(client.get('/api/me').json()['vault']['id'],9)

    def test_duplicate_paths_keep_vault_identity_filter_and_pagination(self):
        _, first_version = self.seed('same', local=False, archive=True, verified=True)
        seed_vault(self.connection, id=10, name='Second', source_root='/second')
        second = self._cloud_only(10, 'photos/same.jpg', 'second-copy', 'DEEP_ARCHIVE', 'b' * 64)
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
