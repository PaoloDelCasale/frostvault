"""DB-free aggregate/API contract checks; PostgreSQL coverage is in PR CI."""
from contextlib import contextmanager
from datetime import datetime, timezone
import re
import unittest
from unittest.mock import Mock, patch

from app import main
from app.routes.deps import current_user
from app.services.path_rules import path_matches, sql_path_regex
from app.services.upload_activity import upload_activity
from tests.fixtures import http_client


class UploadActivityTests(unittest.TestCase):
    def test_informational_path_patterns_follow_admission_globs(self):
        paths = ['a.jpg', 'photos/a.jpg', 'photos/sub/a.jpg', 'a/b/c', 'x/a/b/c/y',
                 'foo', 'x/foo/y', '.DS_Store', 'x/._foo', 'a\nb.jpg', 'a[1].jpg',
                 'foo/a/bar/x', 'x/foo/a/bar/x']
        patterns = ['*.jpg', 'photos/**', '**/*.jpg', '**/a/b/**', 'a*b*c',
                    'foo*bar', 'foo', '.DS_Store', '._*', '?', '[!x]*', 'a[[]1].jpg']
        for pattern in patterns:
            regex = re.compile(sql_path_regex(pattern))
            for path in paths:
                observed = bool(regex.search(path))
                if '**' not in pattern:
                    observed |= any(bool(regex.search(part)) for part in path.split('/'))
                self.assertEqual(observed, path_matches(path, pattern), (path, pattern))

    def test_only_aggregate_rows_leave_sql_and_no_write_or_provider_calls(self):
        connection = Mock()
        connection.execute.return_value.fetchall.side_effect = [
            [{'id': 9, 'name': 'Photos', 'lifecycle': 'active'}],
            [{'vault_id': 9, 'stage': 'uploading', 'files': 1500, 'bytes': 15000, 'transferred_bytes': 4000}],
            [{'stage': 'stability_wait', 'files': 2000, 'bytes': 24000,
              'next_stable_at': datetime(2026, 11, 6, 10, 5, tzinfo=timezone.utc)}],
        ]
        connection.execute.return_value.fetchone.return_value = None  # default policy
        result = upload_activity(connection, now=datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
        self.assertEqual(result['global']['uploading']['files'], 1500)
        self.assertEqual(result['global']['stability_wait']['files'], 2000)
        self.assertEqual(len(result['vaults']), 1)
        # #755: speeds come from the in-memory sampler, not from this query.
        self.assertNotIn('rate', result['global'])
        self.assertFalse(result['vaults'][0]['paused'])
        self.assertEqual(connection.execute.call_count, 4)
        queries = [call.args[0].strip() for call in connection.execute.call_args_list]
        self.assertTrue(all(query.startswith(('SELECT', 'WITH')) for query in queries))
        self.assertIn('GROUP BY', queries[1])
        self.assertIn('GROUP BY', queries[3])
        self.assertIn('f.local_content_observed_at', queries[3])
        self.assertNotIn('pending_since', queries[3])

    def test_job_stage_query_reads_the_preflight_gate_in_the_same_aggregate(self):
        connection = Mock()
        connection.execute.return_value.fetchall.side_effect = [[], [], []]
        upload_activity(connection, now=datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
        stage_query = connection.execute.call_args_list[1].args[0]
        # No extra round trip: a blocked gate demotes zero-byte 'uploading' leases to queued.
        self.assertIn('cloud_preflight_state', stage_query)
        self.assertIn("'blocked'", stage_query)
        self.assertEqual(connection.execute.call_count, 2)  # Vaults, stages

    def test_endpoint_requires_owner_and_is_installation_scoped(self):
        client = http_client()
        self.assertEqual(client.get('/api/upload-activity').status_code, 401)
        # What the aggregate answers for an installation without Vaults.
        stage = {'files': 0, 'bytes': 0, 'transferred_bytes': 0}
        aggregate = {'stability_wait': stage, 'queued': stage, 'uploading': stage, 'verifying': stage,
                     'awaiting_admission_files': 0, 'next_stable_at': None,
                     'bulk_admission': {'requests': 0, 'selected_files': 0, 'processed_files': 0,
                                        'admitted_files': 0}}
        expected = {'observed_at': '2026-11-06', 'global': dict(aggregate), 'vaults': []}
        # And since when a Vault with work has stopped (#759).
        annotated = {'throughput', 'expected_finish', 'stopped_since'}
        @contextmanager
        def connection():
            yield Mock()
        main.app.dependency_overrides[current_user] = lambda: {'id': 1}
        try:
            with patch('app.routes.browse.db', connection), patch('app.services.upload_activity.upload_activity', return_value=expected):
                response = client.get('/api/upload-activity?vault_id=999')
            self.assertEqual(response.status_code, 200)
            # Plus the installation-wide server priority (#713).
            body = response.json()
            # Plus the speeds and expected finish of the sampler (#755).
            self.assertEqual(set(body['global']) - set(aggregate), annotated)
            self.assertIsNone(body['global']['expected_finish'])
            body['global'] = {}
            self.assertEqual(body, {'observed_at': '2026-11-06', 'global': {}, 'vaults': [],
                                    'server_priority': {'throttled': False, 'since': None}})
        finally:
            main.app.dependency_overrides.pop(current_user, None)
