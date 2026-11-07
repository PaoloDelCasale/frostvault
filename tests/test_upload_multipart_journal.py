"""#491: an interrupted multipart upload is aborted only when its UploadId was journaled.

Database-free: the facade journals the UploadId before any part can be sent, and the
proof of absence aborts exactly the journaled ids and nothing else. The database-backed
flow is covered in ``test_upload_multipart_abort``.
"""
import json
import unittest
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from app.services import upload_provenance as provenance
from app.services.s3_upload import _UploadClient
from tests.upload_provenance_provider import VersionedProvider

BUCKET, KEY = "bucket", "docs/report.txt"


def intent(inventory=None):
    pinned = {"bucket": BUCKET, "key": KEY, "provider": provenance.provider_identity(VersionedProvider())}
    return {"target_json": json.dumps(pinned), "inventory_json": provenance.canonical(inventory or {"versions": [], "markers": []}),
            "token": "token", "snapshot_json": "{}"}


class MultipartFacadeJournalTests(unittest.TestCase):
    def facade(self, events, create="up-1"):
        client = MagicMock(spec=["create_multipart_upload", "complete_multipart_upload", "abort_multipart_upload"])
        client.create_multipart_upload.return_value = {"UploadId": create}
        client.complete_multipart_upload.return_value = {"VersionId": "v1"}
        client.abort_multipart_upload.return_value = {}
        return client, _UploadClient(client, on_multipart=lambda event, upload_id: events.append((event, upload_id)))

    def test_the_upload_id_is_journaled_when_created_and_when_completed(self):
        events = []
        client, facade = self.facade(events)
        facade.create_multipart_upload(Bucket=BUCKET, Key=KEY)
        self.assertEqual(events, [("created", "up-1")])
        facade.complete_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId="up-1", MultipartUpload={"Parts": []})
        self.assertEqual(events, [("created", "up-1"), ("closed", "up-1")])
        self.assertIsNone(facade.pending)

    def test_an_abort_closes_the_journal_and_a_missing_upload_counts_as_gone(self):
        events = []
        client, facade = self.facade(events)
        facade.create_multipart_upload(Bucket=BUCKET, Key=KEY)
        client.abort_multipart_upload.side_effect = ClientError({"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload")
        facade.abort_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId="up-1")
        self.assertEqual(events[-1], ("closed", "up-1"))

    def test_a_refused_abort_keeps_the_row_open(self):
        events = []
        client, facade = self.facade(events)
        facade.create_multipart_upload(Bucket=BUCKET, Key=KEY)
        client.abort_multipart_upload.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "AbortMultipartUpload")
        with self.assertRaises(ClientError):
            facade.abort_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId="up-1")
        self.assertEqual(events, [("created", "up-1")])

    def test_an_unjournaled_upload_is_aborted_at_once_and_never_continues(self):
        def journal(event, upload_id):
            raise RuntimeError("database lost")
        client = MagicMock(spec=["create_multipart_upload", "abort_multipart_upload"])
        client.create_multipart_upload.return_value = {"UploadId": "up-1"}
        client.abort_multipart_upload.return_value = {}
        facade = _UploadClient(client, on_multipart=journal)
        with self.assertRaises(RuntimeError):
            facade.create_multipart_upload(Bucket=BUCKET, Key=KEY)
        client.abort_multipart_upload.assert_called_once_with(Bucket=BUCKET, Key=KEY, UploadId="up-1")
        self.assertIsNone(facade.pending)

    def test_a_journal_close_failure_never_fails_the_upload(self):
        def journal(event, upload_id):
            if event == "closed":
                raise RuntimeError("database lost")
        client = MagicMock(spec=["create_multipart_upload", "complete_multipart_upload"])
        client.create_multipart_upload.return_value = {"UploadId": "up-1"}
        client.complete_multipart_upload.return_value = {"VersionId": "v1"}
        facade = _UploadClient(client, on_multipart=journal)
        facade.create_multipart_upload(Bucket=BUCKET, Key=KEY)
        self.assertEqual(facade.complete_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId="up-1", MultipartUpload={"Parts": []})["VersionId"], "v1")


class ExactAbortInProofOfAbsenceTests(unittest.TestCase):
    def setUp(self):
        self.provider = VersionedProvider()
        self.closed = []

    def reconcile(self, owned):
        return provenance.reconcile(self.provider, intent(), None, lambda: None,
                                    owned_multiparts=owned, on_multipart_aborted=self.closed.append)

    def test_the_journaled_upload_is_aborted_and_absence_is_then_proven(self):
        self.provider.multiparts.append((KEY, "own"))
        with self.assertRaises(provenance.ProvenAbsent):
            self.reconcile(["own"])
        self.assertEqual(self.provider.aborted, [(KEY, "own")])
        self.assertEqual(self.closed, ["own"])
        self.assertEqual(self.provider.multiparts, [])

    def test_an_untraceable_upload_is_never_aborted(self):
        self.provider.multiparts.append((KEY, "foreign"))
        with self.assertRaises(provenance.UploadBlocked) as caught:
            self.reconcile([])
        self.assertEqual(caught.exception.reason, "ambiguous_send")
        self.assertEqual(self.provider.aborted, [])
        self.assertEqual(self.provider.multiparts, [(KEY, "foreign")])

    def test_only_the_journaled_id_is_aborted_and_a_foreign_one_keeps_the_send_ambiguous(self):
        self.provider.multiparts += [(KEY, "own"), (KEY, "foreign")]
        with self.assertRaises(provenance.UploadBlocked) as caught:
            self.reconcile(["own"])
        self.assertEqual(caught.exception.reason, "ambiguous_send")
        self.assertEqual(self.provider.aborted, [(KEY, "own")])
        self.assertEqual(self.provider.multiparts, [(KEY, "foreign")])

    def test_a_journaled_id_never_aborts_another_key(self):
        self.provider.multiparts.append((KEY + ".other", "own"))
        with self.assertRaises(provenance.ProvenAbsent):
            self.reconcile(["own"])
        self.assertEqual(self.provider.aborted, [])

    def test_an_already_gone_upload_is_not_an_error(self):
        self.provider.multiparts.append((KEY, "own"))
        original = self.provider.abort_multipart_upload
        def lost(**kwargs):
            self.provider.multiparts.clear()
            return original(**kwargs)
        self.provider.abort_multipart_upload = lost
        with self.assertRaises(provenance.ProvenAbsent):
            self.reconcile(["own"])
        self.assertEqual(self.closed, ["own"])

    def test_a_refused_abort_blocks_the_file_without_touching_the_gate(self):
        self.provider.multiparts.append((KEY, "own"))
        def denied(**kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "provider text"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "AbortMultipartUpload")
        self.provider.abort_multipart_upload = denied
        from unittest.mock import patch
        with patch.object(provenance, "_bucket_still_ready", return_value=True), self.assertRaises(provenance.UploadBlocked) as caught:
            self.reconcile(["own"])
        self.assertEqual(caught.exception.reason, "write_permission_denied")
        self.assertEqual(self.closed, [])
        self.assertEqual(self.provider.multiparts, [(KEY, "own")])

    def test_a_completed_write_is_never_aborted_over(self):
        # The key already has a version: the multipart branch is not reached.
        self.provider.multiparts.append((KEY, "own"))
        self.provider.add(KEY, b"another writer", {})
        with self.assertRaises(provenance.UploadBlocked):
            provenance.reconcile(self.provider, intent(), None, lambda: None, owned_multiparts=["own"])
        self.assertEqual(self.provider.aborted, [])


if __name__ == "__main__":
    unittest.main()
