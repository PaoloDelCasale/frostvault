"""Owner-only whole-Vault move HTTP contract on the rebuilt model (#595)."""
import json
import unittest

from app import main
from tests import test_vault_moves as fixture


class VaultMoveHttpTests(fixture.CopyWorkerFixture):
    owner = {}

    def setUp(self):
        super().setUp()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET storage_plan=%s::jsonb WHERE id=2",
                               (json.dumps(fixture.PLAN),))
        self.current = self.protected()
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers
        self.payload = {'target_class': 'DEEP_ARCHIVE', 'acknowledge_costs': True}

    def storage_class_jobs(self) -> int:
        return len(self.rows("SELECT id FROM jobs WHERE action='storage-class'"))

    def test_preview_and_whole_vault_admission_need_no_unrelated_reauthentication(self):
        response = self.client.get('/api/vaults/2/move')
        self.assertEqual(response.status_code, 200, response.text)
        targets = response.json()['targets']
        self.assertEqual([t['target_class'] for t in targets], ['STANDARD_IA', 'GLACIER', 'DEEP_ARCHIVE'])
        self.assertEqual(targets[-1]['minimum_days'], 180)
        response = self.client.post('/api/vaults/2/move', json=self.payload,
                                    headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['item_count'], 1)
        again = self.client.post('/api/vaults/2/move', json=self.payload,
                                 headers=self.headers)
        self.assertEqual(again.status_code, 409, again.text)

    def test_no_per_file_folder_version_warmer_or_unacknowledged_action(self):
        for extra in ({'path': 'report.txt'}, {'paths': ['report.txt']},
                      {'archive_version_id': str(self.current['id'])},
                      {'is_directory': True}, {'whole_vault': False},
                      # The path names the Vault: the body no longer repeats it (ADR-0056).
                      {'vault_id': 2}):
            response = self.client.post('/api/vaults/2/move',
                                        json={**self.payload, **extra}, headers=self.headers)
            self.assertEqual(response.status_code, 422, response.text)
        for payload in ({'target_class': 'STANDARD', 'acknowledge_costs': True},
                        {'target_class': 'DEEP_ARCHIVE', 'acknowledge_costs': False}):
            response = self.client.post('/api/vaults/2/move', json=payload,
                                        headers=self.headers)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn('/api/storage-class', main.app.openapi()['paths'])
        self.assertEqual(self.storage_class_jobs(), 0)

    def test_authentication_and_csrf_gate_movement(self):
        response = self.client.post('/api/vaults/2/move', json=self.payload)
        self.assertEqual(response.status_code, 403)
        self.client.cookies.clear()
        response = self.client.post('/api/vaults/2/move', json=self.payload,
                                    headers=self.headers)
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.storage_class_jobs(), 0)


if __name__ == "__main__":
    unittest.main()
