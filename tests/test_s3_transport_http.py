"""Real SDK against a loopback-only disposable provider, never live S3/DB."""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import tempfile
import threading
import unittest
from collections import Counter
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import zlib

from botocore.exceptions import BotoCoreError, ClientError

from app.services.s3_clients import shutdown_s3_clients
from app.services.s3_upload import upload_file
from app.services.upload_snapshot import MULTIPART_THRESHOLD, prepare_upload_snapshot
from app.storage import s3_client


class SDKTransportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = Counter()
        self.bodies = []
        self.failures = {}
        owner = self
        class Provider(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def handle_request(self):
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                operation = ("abort" if self.command == "DELETE" else
                             "create" if "uploads" in query else
                             "complete" if self.command == "POST" else
                             "part" if "partNumber" in query else "put")
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.calls[operation] += 1
                owner.bodies.append((operation, body, dict(self.headers)))
                failure = owner.failures.get(operation)
                if failure == "disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
                    return
                if failure == "once" and owner.calls[operation] == 1 or failure == "always":
                    status, payload = 503, b"<Error><Code>ServiceUnavailable</Code></Error>"
                elif failure == "digest":
                    status, payload = 400, b"<Error><Code>BadDigest</Code></Error>"
                else:
                    status = 200
                    payload = (b"<InitiateMultipartUploadResult><UploadId>mpu</UploadId></InitiateMultipartUploadResult>"
                               if operation == "create" else
                               b'<CompleteMultipartUploadResult><ETag>"complete"</ETag></CompleteMultipartUploadResult>'
                               if operation == "complete" else b"")
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                if status == 200:
                    self.send_header("ETag", '"part"')
                    if operation in {"put", "complete"}:
                        self.send_header("x-amz-version-id", "exact")
                    if operation in {"put", "part"}:
                        # Compute independent wire evidence, never reflect a
                        # request header into the response (CodeQL28/29).
                        if self.headers.get("x-amz-checksum-sha256"):
                            checksum = ("d3Jvbmc=" if failure == "wrong_checksum" else
                                        base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"))
                            self.send_header("x-amz-checksum-sha256", checksum)
                        if self.headers.get("x-amz-checksum-crc32"):
                            checksum = base64.b64encode((zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")).decode("ascii")
                            self.send_header("x-amz-checksum-crc32", checksum)
                self.end_headers()
                self.wfile.write(payload)
            do_PUT = do_POST = do_DELETE = handle_request
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        env = patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_ENDPOINT_URL": f"http://127.0.0.1:{server.server_port}",
            "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutdown_s3_clients)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "content"

    def upload(self, payload, **kwargs):
        self.source.write_bytes(payload)
        snapshot = prepare_upload_snapshot(self.source, lambda: None)[0]
        return upload_file(s3_client(), write_client=s3_client(non_idempotent=True),
                           source=self.source, snapshot=snapshot, bucket="bucket", key="key",
                           check_active=lambda: None, on_progress=kwargs.pop("on_progress", lambda _: None), **kwargs)

    def test_fixture_computes_response_checksums_instead_of_reflecting_headers(self):
        endpoint = urlsplit(os.environ["AWS_ENDPOINT_URL"])
        client = HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
        self.addCleanup(client.close)
        body = b"independent wire evidence"
        client.request("PUT", "/bucket/key", body, headers={
            "x-amz-checksum-sha256": "caller-controlled",
            "x-amz-checksum-crc32": "caller-controlled",
        })
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("x-amz-checksum-sha256"),
                         base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"))
        self.assertEqual(response.getheader("x-amz-checksum-crc32"),
                         base64.b64encode((zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")).decode("ascii"))
        response.read()

    def test_small_put_wire_checksum_progress_and_acknowledgment(self):
        progress, ack = [], []
        response = self.upload(b"snapshot A", on_progress=progress.append, on_acknowledged=ack.append)
        self.assertEqual(self.calls, {"put": 1})
        self.assertEqual(response["VersionId"], "exact")
        self.assertEqual(ack, [response])
        self.assertEqual(sum(progress), len(b"snapshot A"))
        self.assertEqual(self.bodies[0][1], b"snapshot A")
        self.assertEqual(response["ChecksumSHA256"], "d6b2nUtIsucFL18mz0x3fV0XHNaWYnknXiU/bTbCTBs=")
        self.assertEqual(self.bodies[0][2]["x-amz-checksum-sha256"], "d6b2nUtIsucFL18mz0x3fV0XHNaWYnknXiU/bTbCTBs=")

    def test_legacy_crc32_put_retains_full_object_proof_and_progress(self):
        self.source.write_bytes(b"legacy")
        progress = []
        response = upload_file(s3_client(), write_client=s3_client(non_idempotent=True),
                               source=self.source, bucket="bucket", key="key",
                               check_active=lambda: None, on_progress=progress.append)
        self.assertEqual(self.calls, {"put": 1})
        self.assertEqual(response["VersionId"], "exact")
        self.assertEqual(self.bodies[0][1], b"legacy")
        self.assertEqual(sum(progress), len(b"legacy"))
        self.assertIn("ChecksumCRC32", response)

    def test_mutators_have_one_wire_attempt_even_for_retryable_provider_failure(self):
        self.failures.update(put="always", create="always", complete="always")
        writer = s3_client(non_idempotent=True)
        calls = (("put", lambda: writer.put_object(Bucket="bucket", Key="key", Body=b"a")),
                 ("create", lambda: writer.create_multipart_upload(Bucket="bucket", Key="key")),
                 ("complete", lambda: writer.complete_multipart_upload(Bucket="bucket", Key="key", UploadId="mpu",
                                                                       MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": "part"}]})))
        for operation, call in calls:
            with self.subTest(operation=operation), self.assertRaises(ClientError) as caught:
                call()
            self.assertEqual(caught.exception.response["ResponseMetadata"]["RetryAttempts"], 0)
            self.assertEqual(self.calls[operation], 1)

    def test_lost_put_response_is_not_replayed_or_mistaken_for_absence(self):
        self.failures["put"] = "disconnect"
        ack = []
        with self.assertRaises(BotoCoreError) as caught:
            self.upload(b"snapshot A", on_acknowledged=ack.append)
        self.assertEqual(self.calls, {"put": 1})
        self.assertEqual(ack, [])
        self.assertFalse(hasattr(caught.exception, "upload_no_write_operation"))
        self.assertFalse(hasattr(caught.exception, "upload_response"))

    def test_multipart_retries_same_part_with_rewind_and_completes_only_once(self):
        self.failures["part"] = "once"
        payload = b"a" * MULTIPART_THRESHOLD
        progress = []
        self.assertEqual(self.upload(payload, on_progress=progress.append)["VersionId"], "exact")
        self.assertEqual(self.calls, {"create": 1, "part": 2, "complete": 1})
        attempts = [body for operation, body, _ in self.bodies if operation == "part"]
        self.assertEqual(attempts, [payload, payload])
        self.assertEqual(sum(progress), len(payload))
        # Managed-transfer aggregation may absorb negative deltas; the contract
        # is complete retransmission with a correct final progress total.

    def test_wrong_part_checksum_aborts_before_completion(self):
        self.failures["part"] = "wrong_checksum"
        with self.assertRaisesRegex(RuntimeError, "part checksum mismatch"):
            self.upload(b"a" * MULTIPART_THRESHOLD)
        self.assertEqual(self.calls, {"create": 1, "part": 1, "abort": 1})

    def test_multipart_bad_digest_aborts_without_completion_or_object_delete(self):
        self.failures["part"] = "digest"
        with self.assertRaises(ClientError) as caught:
            self.upload(b"a" * MULTIPART_THRESHOLD)
        self.assertEqual(caught.exception.operation_name, "UploadPart")
        self.assertEqual(caught.exception.response["Error"]["Code"], "BadDigest")
        self.assertEqual(self.calls, {"create": 1, "part": 1, "abort": 1})
