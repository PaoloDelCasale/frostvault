"""DB-independent contracts of the durable frontier; PG16 semantics live in frontier tests."""
import unittest
from unittest.mock import MagicMock, patch

from app.services import upload_remediation_discovery as discovery


STAMP = '2026-11-12T12:00:00+00:00'


class DurableDiscoveryQueryTests(unittest.TestCase):
    def test_targeted_lookup_has_three_indexable_equalities_without_nullable_or(self):
        sql, params = discovery.obligation_query(vault_sql='2', stamp=STAMP, file_id='exact-file', limit=10)
        self.assertEqual(params, ('exact-file',) * 3)
        for predicate in ('i.vault_file_id=%s', 'av.vault_file_id=%s', 'vf.id=%s'):
            self.assertIn(predicate, sql)
        self.assertNotIn('IS NULL OR', sql)
        self.assertNotIn('created_at<=', sql, 'A targeted admission may not miss a fresh send')
        self.assertNotIn('upload_remediation_facts', sql)

    def test_global_send_grace_is_bound_without_wrapping_the_indexed_column(self):
        sql, params = discovery.obligation_query(vault_sql='v.id', stamp=STAMP, limit=1)
        self.assertEqual(params, ('2026-11-12T11:59:50+00:00',))
        self.assertIn('i.created_at<=%s', sql)
        self.assertIn('i.send_started AND NOT i.settled', sql)
        self.assertNotIn('created_at::', sql)
        self.assertEqual(sql.count('LIMIT 1'), 4)  # three sources + exact verified keeper

    def test_active_and_enrolled_filters_precede_each_bounded_source_limit(self):
        sql, _ = discovery.obligation_query(vault_sql='2', stamp=STAMP, limit=3)
        branches = sql.split(' UNION ALL ')
        self.assertEqual(len(branches), 3)
        for branch in branches:
            self.assertIn("active.status NOT IN ('failed','cancelled','completed')", branch)
            self.assertIn('NOT EXISTS (SELECT 1 FROM upload_remediations', branch)
            self.assertTrue(branch.endswith('ORDER BY obligation_at,obligation_key LIMIT 3)'))
        self.assertNotIn("r.state IN ('pending'", sql, 'Cancelled/blocked episodes must also leave the frontier')
        self.assertIn("r.state<>'resolved'", branches[2])
        self.assertNotIn('resolved_at', branches[2], 'A durable block cannot be lost to timestamp ordering')

    def test_discovery_cannot_elect_an_unknown_or_unverified_keeper(self):
        sql, _ = discovery.obligation_query(vault_sql='2', stamp=STAMP)
        self.assertIn("keeper.integrity='verified' AND keeper.availability='available'", sql)
        self.assertIn("av.upload_snapshot_json IS NOT NULL", sql)
        self.assertIn("evidence.upload_plaintext_sha256 ~ '^[0-9a-f]{64}$'", sql)
        self.assertNotIn('provider_version_id', sql)
        self.assertNotIn('etag', sql)

    def test_expired_timeslice_enrolls_nothing_and_consumes_no_source(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {'intent_id': 'intent', 'archive_version_id': None}]
        with patch.object(discovery, 'monotonic', return_value=2), patch.object(discovery, 'enroll') as enroll:
            discovery.discover(connection, {'id': 2}, STAMP, 1, deadline=1)
        enroll.assert_not_called()
        self.assertEqual(connection.execute.call_count, 1)
        self.assertTrue(connection.execute.call_args.args[0].startswith('SELECT'))

    def test_discovery_passes_exact_evidence_identity_and_never_deletes_the_source(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {'intent_id': 'intent', 'archive_version_id': 'version'}]
        vault = {'id': 2}
        with patch.object(discovery, 'enroll') as enroll:
            discovery.discover(connection, vault, STAMP, 1)
        enroll.assert_called_once_with(connection, None, vault, STAMP, intent_id='intent', version_id='version')
        self.assertEqual(connection.execute.call_count, 1)
        self.assertNotIn('DELETE', connection.execute.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
