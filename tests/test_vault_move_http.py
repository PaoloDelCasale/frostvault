"""Owner-only whole-Vault move HTTP contract on the rebuilt model (#595)."""
import json
import unittest

from app import main
from tests import test_vault_moves as fixture
from tests.fixtures import create_owner, http_client, override_settings, sign_in


class VaultMoveHttpTests(fixture.CopyWorkerFixture):
    def setUp(self):
        super().setUp()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET storage_plan=%s::jsonb WHERE id=2",
                               (json.dumps(fixture.PLAN),))
        self.current = self.protected()
        with self.db() as connection:
            create_owner(connection)
        self.enterContext(override_settings(cookie_secure=False))
        self.client = http_client()
        self.headers = sign_in(self.client, self.database).csrf_headers
        self.payload = {'vault_id': 2, 'target_class': 'DEEP_ARCHIVE', 'acknowledge_costs': True}

    def storage_class_jobs(self) -> int:
        return len(self.rows("SELECT id FROM jobs WHERE action='storage-class'"))

    def test_preview_and_whole_vault_admission_need_no_unrelated_reauthentication(self):
        response = self.client.get('/api/vault/move', params={'vault_id': 2})
        self.assertEqual(response.status_code, 200, response.text)
        targets = response.json()['targets']
        self.assertEqual([t['target_class'] for t in targets], ['STANDARD_IA', 'GLACIER', 'DEEP_ARCHIVE'])
        self.assertEqual(targets[-1]['minimum_days'], 180)
        response = self.client.post('/api/vault/move', params={'vault_id': 2}, json=self.payload,
                                    headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['item_count'], 1)
        again = self.client.post('/api/vault/move', params={'vault_id': 2}, json=self.payload,
                                 headers=self.headers)
        self.assertEqual(again.status_code, 409, again.text)

    def test_no_per_file_folder_version_warmer_or_unacknowledged_action(self):
        for extra in ({'path': 'report.txt'}, {'paths': ['report.txt']},
                      {'archive_version_id': str(self.current['id'])},
                      {'is_directory': True}, {'whole_vault': False}):
            response = self.client.post('/api/vault/move', params={'vault_id': 2},
                                        json={**self.payload, **extra}, headers=self.headers)
            self.assertEqual(response.status_code, 422, response.text)
        for payload in ({'vault_id': 2, 'target_class': 'STANDARD', 'acknowledge_costs': True},
                        {'vault_id': 2, 'target_class': 'DEEP_ARCHIVE', 'acknowledge_costs': False},
                        {'vault_id': 3, 'target_class': 'DEEP_ARCHIVE', 'acknowledge_costs': True}):
            response = self.client.post('/api/vault/move', params={'vault_id': 2}, json=payload,
                                        headers=self.headers)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn('/api/storage-class', main.app.openapi()['paths'])
        self.assertEqual(self.storage_class_jobs(), 0)

    def test_authentication_and_csrf_gate_movement(self):
        response = self.client.post('/api/vault/move', params={'vault_id': 2}, json=self.payload)
        self.assertEqual(response.status_code, 403)
        self.client.cookies.clear()
        response = self.client.post('/api/vault/move', params={'vault_id': 2}, json=self.payload,
                                    headers=self.headers)
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.storage_class_jobs(), 0)


if __name__ == "__main__":
    unittest.main()
