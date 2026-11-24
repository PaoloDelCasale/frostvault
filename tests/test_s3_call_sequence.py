"""The S3 requests of upload, verification and copy, in number and order (#792).

Real admission, claim and ``storage.process_job`` on a disposable PostgreSQL 16
database; the provider is the synthetic ``VersionedProvider`` with every
request recorded, together with each client the worker asks for (its retry
policy). The sequences below are the ones the worker sent before the S3 module
was unified: a refactor of that module must leave them identical.

The shared clients' own configuration (retries, timeouts, connection pool,
reuse) is pinned on real SDK clients, without network.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app import storage
from app.services.s3_upload import upload_file
from tests.test_rename_sync import CopyWorkerFixture

# What the worker may ask the provider; ``upload`` is the transport of one PUT.
_REQUESTS = (
    "upload", "head_object", "get_object", "get_object_tagging", "copy_object",
    "delete_object", "delete_objects", "list_object_versions", "list_multipart_uploads",
    "abort_multipart_upload", "restore_object",
)


class S3CallSequenceTests(CopyWorkerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple] = []
        for name in _REQUESTS:
            setattr(self.provider, name, self._recorded(name, getattr(self.provider, name)))
        self.enterContext(patch("app.storage.s3_client", side_effect=self._client))
        self.enterContext(patch("app.storage.upload_file", side_effect=self.provider.upload))

    def _client(self, **kwargs):
        self.calls.append(("s3_client", *sorted(kwargs.items())))
        return self.provider

    def _recorded(self, name, request):
        def call(*args, **kwargs):
            # The arguments by name, and the values that choose what S3 does.
            chosen = tuple((key, kwargs[key]) for key in ("VersionId", "ChecksumMode", "StorageClass",
                                                          "MetadataDirective") if key in kwargs)
            self.calls.append((name, tuple(sorted(kwargs)), chosen))
            return request(*args, **kwargs)
        return call

    def sequence(self) -> list[tuple]:
        """The recorded calls, then forgotten: one sequence per Job."""
        calls, self.calls = self.calls, []
        return calls

    def test_upload_with_the_provider_checksum(self) -> None:
        job_id = self.queue("s3_checksum")
        self.assertTrue(self.run_job(job_id))
        self.assert_current(job_id, b"immutable content A")
        self.assertEqual(self.sequence(), UPLOAD_S3_CHECKSUM)

    def test_upload_verified_by_reading_the_version_back(self) -> None:
        job_id = self.queue("read_back")
        self.run_job(job_id)
        self.assert_current(job_id, b"immutable content A")
        self.assertEqual(self.sequence(), UPLOAD_READ_BACK)

    def test_a_new_upload_removes_the_superseded_version(self) -> None:
        self.protected()
        self.sequence()
        self.write(b"different content B")
        job_id = self.queue()
        self.run_job(job_id)
        self.assert_current(job_id, b"different content B")
        self.assertEqual(self.sequence(), UPLOAD_REPLACING)

    def test_storage_class_copy(self) -> None:
        self.protected()
        self.sequence()
        job_id = self.admit("storage-class", "report.txt", target_storage_class="STANDARD_IA")
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.sequence(), STORAGE_CLASS_COPY)

    def test_rename_copy(self) -> None:
        from app.services.file_renames import FileRenames
        self.protected()
        self.sequence()
        target = self.source / "renamed.txt"
        os.replace(self.file, target)
        self.file = target
        with self.db() as connection:
            FileRenames(connection).rename_file(
                self.file_id, new_path="renamed.txt", changed_at=storage.now_iso(), vault_id=2)
        job_id = self.admit("rename", "renamed.txt")
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.sequence(), RENAME_COPY)


class SharedClientConfigurationTests(unittest.TestCase):
    """Retries, timeouts, pool and reuse of the three shared clients."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:19000", "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        }, clear=True))

    def test_each_policy_keeps_its_configuration_and_its_one_client(self) -> None:
        observed = {}
        for name, kwargs in (("read", {}), ("write", {"non_idempotent": True}),
                             ("preflight", {"preflight": True})):
            client = storage.s3_client(**kwargs)
            self.assertIs(storage.s3_client(**kwargs), client)
            config = client.meta.config
            observed[name] = (config.retries, config.connect_timeout, config.read_timeout,
                              config.max_pool_connections, client.meta.region_name)
        region = storage.settings.aws_region
        self.assertEqual(observed, {
            "read": ({"mode": "standard", "total_max_attempts": 5}, 60, 60, 10, region),
            "write": ({"mode": "standard", "total_max_attempts": 1}, 60, 60, 10, region),
            "preflight": ({"mode": "standard", "total_max_attempts": 1}, 2, 3, 10, region),
        })
        self.assertEqual(len({id(storage.s3_client(**k)) for k in (
            {}, {"non_idempotent": True}, {"preflight": True})}), 3)
        self.assertIsNot(storage.s3_client(region="eu-north-1"), storage.s3_client())
        self.assertEqual(storage.s3_client(region="eu-north-1").meta.region_name, "eu-north-1")

    def test_placeholder_credentials_are_refused_before_any_client(self) -> None:
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            with self.subTest(name), patch.dict(os.environ, {name: "REPLACE_ME"}):
                with self.assertRaisesRegex(RuntimeError, "AWS credentials are not configured in the .env file"):
                    storage.s3_client()

    def test_upload_file_is_the_transport_the_worker_uses(self) -> None:
        self.assertIs(storage.upload_file, upload_file)


# The sequences sent by the worker before #792, recorded with the helper above.
UPLOAD_S3_CHECKSUM = [('s3_client',),
 ('s3_client',),
 ('s3_client', ('non_idempotent', True)),
 ('upload',
  ('bucket',
   'check_active',
   'key',
   'max_bandwidth',
   'metadata',
   'on_acknowledged',
   'on_multipart',
   'on_progress',
   'payload',
   'snapshot',
   'source',
   'write_client'),
  ()),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('list_object_versions', ('Bucket', 'Prefix'), ())]

UPLOAD_READ_BACK = [('s3_client',),
 ('s3_client',),
 ('s3_client', ('non_idempotent', True)),
 ('upload',
  ('bucket',
   'check_active',
   'key',
   'max_bandwidth',
   'metadata',
   'on_acknowledged',
   'on_multipart',
   'on_progress',
   'payload',
   'snapshot',
   'source',
   'write_client'),
  ()),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('s3_client',),
 ('get_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('list_object_versions', ('Bucket', 'Prefix'), ())]

UPLOAD_REPLACING = [('s3_client',),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('s3_client',),
 ('s3_client', ('non_idempotent', True)),
 ('upload',
  ('bucket',
   'check_active',
   'key',
   'max_bandwidth',
   'metadata',
   'on_acknowledged',
   'on_multipart',
   'on_progress',
   'payload',
   'snapshot',
   'source',
   'write_client'),
  ()),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-2'),)),
 ('head_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-2'),)),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('delete_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ())]

STORAGE_CLASS_COPY = [('s3_client',),
 ('head_object',
  ('Bucket', 'ChecksumMode', 'Key', 'VersionId'),
  (('VersionId', 'version-1'), ('ChecksumMode', 'ENABLED'))),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('copy_object',
  ('Bucket',
   'ChecksumAlgorithm',
   'CopySource',
   'Key',
   'Metadata',
   'MetadataDirective',
   'StorageClass',
   'TaggingDirective'),
  (('StorageClass', 'STANDARD_IA'), ('MetadataDirective', 'REPLACE'))),
 ('head_object',
  ('Bucket', 'ChecksumMode', 'Key', 'VersionId'),
  (('VersionId', 'version-2'), ('ChecksumMode', 'ENABLED'))),
 ('head_object',
  ('Bucket', 'ChecksumMode', 'Key', 'VersionId'),
  (('VersionId', 'version-2'), ('ChecksumMode', 'ENABLED'))),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('delete_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ())]

RENAME_COPY = [('s3_client',),
 ('head_object',
  ('Bucket', 'ChecksumMode', 'Key', 'VersionId'),
  (('VersionId', 'version-1'), ('ChecksumMode', 'ENABLED'))),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('copy_object',
  ('Bucket',
   'ChecksumAlgorithm',
   'CopySource',
   'Key',
   'Metadata',
   'MetadataDirective',
   'StorageClass',
   'TaggingDirective'),
  (('StorageClass', 'STANDARD'), ('MetadataDirective', 'REPLACE'))),
 ('head_object',
  ('Bucket', 'ChecksumMode', 'Key', 'VersionId'),
  (('VersionId', 'version-2'), ('ChecksumMode', 'ENABLED'))),
 ('head_object',
  ('Bucket', 'ChecksumMode', 'Key', 'VersionId'),
  (('VersionId', 'version-2'), ('ChecksumMode', 'ENABLED'))),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('delete_object', ('Bucket', 'Key', 'VersionId'), (('VersionId', 'version-1'),)),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ()),
 ('list_object_versions', ('Bucket', 'Prefix'), ())]


if __name__ == "__main__":
    unittest.main()
