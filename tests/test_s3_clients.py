"""Process/configuration S3 factory seam; no network or production credentials."""
from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.storage import s3_client
from app.services.s3_clients import shutdown_s3_clients


class S3ClientReuseTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:19000",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(shutdown_s3_clients)

    def test_clients_are_reused_but_mutators_and_reads_have_separate_retry_policies(self):
        reader = s3_client()
        writer = s3_client(non_idempotent=True)
        probe = s3_client(preflight=True)
        self.assertIs(s3_client(), reader)
        self.assertIs(s3_client(non_idempotent=True), writer)
        self.assertIs(s3_client(preflight=True), probe)
        self.assertIsNot(reader, writer)
        self.assertIsNot(writer, probe)
        self.assertEqual(writer.meta.config.retries["total_max_attempts"], 1)
        self.assertGreater(reader.meta.config.retries["total_max_attempts"], 1)
        self.assertEqual(probe.meta.config.retries["total_max_attempts"], 1)
        self.assertEqual(probe.meta.config.connect_timeout, 2)
        self.assertEqual(probe.meta.config.read_timeout, 3)

    def test_environment_changes_do_not_reuse_endpoint_credentials_or_default_session(self):
        first = s3_client()
        with patch.dict(os.environ, {"AWS_ENDPOINT_URL": "http://127.0.0.1:19001",
                                     "AWS_ACCESS_KEY_ID": "different-test-key"}):
            second = s3_client()
            self.assertIsNot(first, second)
            self.assertEqual(second.meta.endpoint_url, "http://127.0.0.1:19001")
            self.assertIs(s3_client(), second)
        self.assertIs(s3_client(), first)
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "rotated-test-secret"}):
            rotated = s3_client()
            self.assertIsNot(rotated, first)
            self.assertEqual(rotated.meta.endpoint_url, first.meta.endpoint_url)
        with patch.dict(os.environ, {"AWS_ENDPOINT_URL_S3": "http://127.0.0.1:19002"}):
            self.assertEqual(s3_client().meta.endpoint_url, "http://127.0.0.1:19002")

    def test_configuration_file_changes_are_not_hidden_by_a_cached_session(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            def configure(port):
                config.write_text(f"[default]\nservices = local\n[services local]\ns3 =\n  endpoint_url = http://127.0.0.1:{port}\n")
            with patch.dict(os.environ, {"AWS_CONFIG_FILE": str(config)}):
                # Configured service endpoints take precedence over the generic
                # endpoint env var only when that env var is absent.
                endpoint = os.environ.pop("AWS_ENDPOINT_URL")
                try:
                    configure(19003)
                    first = s3_client()
                    self.assertEqual(first.meta.endpoint_url, "http://127.0.0.1:19003")
                    configure(19004)
                    second = s3_client()
                    self.assertIsNot(first, second)
                    self.assertEqual(second.meta.endpoint_url, "http://127.0.0.1:19004")
                finally:
                    os.environ["AWS_ENDPOINT_URL"] = endpoint

    def test_configuration_churn_is_bounded_without_closing_a_client_still_in_use(self):
        with patch("app.storage.boto3.client") as factory:
            from unittest.mock import Mock
            factory.side_effect = lambda *a, **kw: Mock()
            first = s3_client()
            for port in range(19010, 19023):
                with patch.dict(os.environ, {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{port}"}):
                    s3_client()
            self.assertIsNot(s3_client(), first, "oldest configuration must leave the bounded cache")
            first.close.assert_not_called()
            shutdown_s3_clients()
            first.close.assert_called_once()

    def test_concurrent_factory_is_serialized_and_override_does_not_reuse_real_client(self):
        from unittest.mock import Mock
        real = s3_client()
        in_factory = threading.Lock()
        created = []
        def create(*args, **kwargs):
            self.assertTrue(in_factory.acquire(blocking=False), "concurrent Session factory")
            try:
                client = Mock()
                created.append(client)
                return client
            finally:
                in_factory.release()
        with patch("app.storage.boto3.client", side_effect=create):
            with ThreadPoolExecutor(max_workers=8) as pool:
                clients = list(pool.map(lambda _: s3_client(), range(32)))
            self.assertEqual(len(created), 1)
            self.assertTrue(all(client is clients[0] for client in clients))
            self.assertIsNot(clients[0], real)
        self.assertIs(s3_client(), real)

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork safety")
    def test_fork_discards_inherited_clients_without_closing_them(self):
        parent = s3_client()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                with patch.object(parent, "close") as inherited_close:
                    child = s3_client()
                    shutdown_s3_clients()
                    ok = child is not parent and inherited_close.call_count == 0
                os.write(write_fd, b"ok" if ok else b"bad")
            except BaseException:
                os.write(write_fd, b"error")
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 20), b"ok")
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)
        self.assertIs(s3_client(), parent)

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork safety")
    def test_fork_while_another_thread_constructs_a_client_does_not_inherit_its_lock(self):
        import select
        import signal
        from unittest.mock import Mock
        creating, release = threading.Event(), threading.Event()
        def create(*args, **kwargs):
            if kwargs["config"].connect_timeout == 2:
                creating.set()
                if not release.wait(5):
                    raise AssertionError("factory was never released")
            return Mock()
        with patch("app.storage.boto3.client", side_effect=create):
            with ThreadPoolExecutor(max_workers=1) as pool:
                held = pool.submit(s3_client, preflight=True)
                self.assertTrue(creating.wait(2))
                read_fd, write_fd = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(read_fd)
                    try:
                        s3_client()
                        shutdown_s3_clients()
                        os.write(write_fd, b"ok")
                    except BaseException:
                        os.write(write_fd, b"error")
                    finally:
                        os.close(write_fd)
                        os._exit(0)
                os.close(write_fd)
                try:
                    ready = select.select([read_fd], [], [], 3)[0]
                    if not ready:
                        os.kill(pid, signal.SIGKILL)
                    self.assertTrue(ready, "child inherited a locked factory")
                    self.assertEqual(os.read(read_fd, 20), b"ok")
                finally:
                    release.set()
                    os.close(read_fd)
                    os.waitpid(pid, 0)
                    held.result(timeout=5)

    def test_upload_does_not_mutate_shared_client_handlers_or_metadata(self):
        import tempfile
        from pathlib import Path
        from botocore.stub import Stubber
        from app.services.s3_upload import upload_file
        from app.services.upload_snapshot import prepare_upload_snapshot, MULTIPART_THRESHOLD
        client = s3_client(non_idempotent=True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "content"
            path.write_bytes(b"a" * MULTIPART_THRESHOLD)
            snapshot = prepare_upload_snapshot(path, lambda: None)[0]
            with Stubber(client) as stub, patch.object(client.meta.events, "register_first") as first, patch.object(
                client.meta.events, "register_last",
            ) as last:
                stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
                stub.add_response("upload_part", {"ETag": "part"})
                stub.add_response("complete_multipart_upload", {"VersionId": "exact"})
                response = upload_file(client, source=path, bucket="bucket", key="key", snapshot=snapshot,
                                       check_active=lambda: None, on_progress=lambda _: None)
                self.assertEqual(response["VersionId"], "exact")
                first.assert_not_called()
                last.assert_not_called()
                stub.assert_no_pending_responses()

    def test_shutdown_closes_once_and_factory_can_start_a_new_generation(self):
        client = s3_client()
        with patch.object(client, "close") as close:
            shutdown_s3_clients()
            shutdown_s3_clients()
            close.assert_called_once()
        self.assertIsNot(s3_client(), client)
