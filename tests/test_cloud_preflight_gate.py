"""Pure policy/worker tests; persistent/concurrent acceptance runs on CI PostgreSQL."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, Mock
import psycopg
from botocore.exceptions import ClientError

from app import storage
from app.services import cloud_preflight_gate as gate


class CloudPreflightPolicyTests(unittest.TestCase):
    def test_ready_cache_from_previous_process_is_not_startup_authority(self):
        now = datetime.now(timezone.utc)
        state = {'status': 'ready', 'next_probe_at': (now + timedelta(seconds=50)).isoformat()}
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {'acquired': True}
        probe = Mock(side_effect=gate.ReadinessFailure('Check AWS credentials'))
        with patch.object(gate, '_ready_pid', None), patch.object(gate, 'snapshot', return_value=state), patch.object(gate.notifications, 'set_problem'):
            gate.check(lambda: connection, probe, now=now)
        probe.assert_called_once()

    def test_publication_transaction_retry_retains_the_read_only_probe_outcome(self):
        now = datetime.now(timezone.utc)
        state = {'status': 'ready', 'next_probe_at': (now + timedelta(seconds=50)).isoformat()}
        for error in (psycopg.errors.DeadlockDetected(), psycopg.errors.SerializationFailure()):
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = {'acquired': True}
            probe = Mock()
            with self.subTest(error=type(error).__name__), patch.object(gate, '_ready_pid', None), patch.object(
                gate, 'snapshot', return_value=state
            ), patch.object(gate.notifications, 'set_problem', side_effect=[error,None]) as notify, patch(
                'app.services.database_retry.time.sleep'
            ):
                self.assertEqual(gate.check(lambda: connection,probe,now=now),state)
                self.assertTrue(gate.dispatch_ready(state,now))
            probe.assert_called_once()
            self.assertEqual(notify.call_count,2)
            self.assertEqual(connection.close.call_count,3)

    def test_structured_auth_inside_inventory_wrapper_suspends_but_numeric_text_does_not(self):
        error = ClientError({'Error': {'Code': 'SignatureDoesNotMatch'},
                             'ResponseMetadata': {'HTTPStatusCode': 403}}, 'ListObjectVersions')
        wrapped = RuntimeError('safe inventory wrapper')
        wrapped.__cause__ = error
        self.assertTrue(gate.is_readiness_error(wrapped))
        for other in (RuntimeError('403 AccessDenied'), psycopg.errors.UniqueViolation('403'),
                      ClientError({'Error': {'Code': 'SlowDown'},'ResponseMetadata': {'HTTPStatusCode': 503}},'HeadObject')):
            self.assertFalse(gate.is_readiness_error(other))

    def test_bad_credentials_invalidate_ready_and_suspend_without_budget_consumption(self):
        job = {'id': 99002, 'action': 'upload', 'status': 'queued', 'retry_count': 7}
        error = ClientError({'Error': {'Code': 'InvalidAccessKeyId', 'Message': 'not for display'},
                             'ResponseMetadata': {'HTTPStatusCode': 403}}, 'HeadObject')
        with patch.object(storage, '_runtime_settings'), patch.object(storage, '_load_claimed_job_status', return_value={'status': 'queued'}), patch.object(
            storage, 'process_upload', side_effect=error
        ), patch.object(storage, 'set_job', return_value=True) as update, patch.object(
            storage, 'schedule_upload_retry'
        ) as retry, patch.object(gate, 'invalidate', create=True) as invalidate:
            self.assertFalse(storage._process_claimed_job(job))
        invalidate.assert_called_once()
        update.assert_called_once_with(job['id'], 'queued', 'S3 readiness blocked; Job remains queued')
        retry.assert_not_called()
        self.assertEqual(job['retry_count'], 7)

    def test_backoff_is_bounded_even_after_long_iam_propagation(self):
        self.assertEqual([gate.reprobe_seconds(i) for i in range(1, 8)], [5, 10, 20, 40, 60, 60, 60])
        self.assertLessEqual(gate.reprobe_seconds(10**9), 60)

    def test_probe_transport_has_short_timeouts_without_changing_transfer_retries(self):
        with patch.object(storage, 'is_placeholder', return_value=False), patch.object(storage.boto3, 'client') as client:
            storage.s3_client(preflight=True)
            config = client.call_args.kwargs['config']
            self.assertEqual((config.connect_timeout, config.read_timeout), (2, 3))
            self.assertEqual(config.retries['total_max_attempts'], 1)
            storage.s3_client()
            self.assertEqual(client.call_args.kwargs['config'].retries['max_attempts'], 4)

    def test_clock_rollback_and_invalid_restart_timestamp_do_not_hold_the_gate(self):
        now = datetime.now(timezone.utc)
        for due in (None, 'invalid', (now + timedelta(hours=1)).isoformat(), (now - timedelta(seconds=1)).isoformat()):
            self.assertTrue(gate.probe_due({'next_probe_at': due}, now))
        self.assertFalse(gate.probe_due({'next_probe_at': (now + timedelta(seconds=60)).isoformat()}, now))

    def test_aborted_readiness_publication_is_suspension_not_a_transfer_retry(self):
        with patch.object(gate, 'check', side_effect=psycopg.errors.SerializationFailure()) as check:
            with self.assertRaises(gate.CloudPreflightBlocked):
                storage.validate_cloud_vault({'s3_bucket': 'bucket'})
            check.assert_called_once()  # No provider replay due to a DB abort.

    def test_blockage_preserves_job_retry_budget_and_never_retries_mutations(self):
        for action, handler in (("upload", "process_upload"), ("recover", "process_recover"),
                                ("rename", "process_rename"), ("free-space", "process_free_space"),
                                ("storage-class", "process_storage_class")):
            job = {"id": 99001, "action": action, "status": "queued", "retry_count": 7}
            with self.subTest(action=action), patch.object(storage, "_runtime_settings"), patch.object(
                storage, "_load_claimed_job_status", return_value={"status": "queued"}
            ), patch.object(storage, handler, side_effect=gate.CloudPreflightBlocked("IAM")), patch.object(
                storage, "set_job", return_value=True
            ) as update, patch.object(storage, "schedule_upload_retry") as retry:
                self.assertFalse(storage._process_claimed_job(job))
                self.assertEqual(job["retry_count"], 7)
                update.assert_called_once_with(job["id"], "queued", "S3 readiness blocked; Job remains queued")
                retry.assert_not_called()
