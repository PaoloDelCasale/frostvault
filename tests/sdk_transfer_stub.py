"""Scripted SDK transfer boundaries for catalog/worker regression tests.

These catalog tests inject upload/read events; wire-level TransferManager,
checksum, response identity and abort behavior are tested in test_s3_upload.
"""
from contextlib import contextmanager
import hashlib
from unittest.mock import patch


@contextmanager
def mock_sdk_upload(*, side_effect=None, return_value=None):
    import app.storage as storage
    def upload(job, *, source, key):
        if side_effect:
            side_effect("upload", str(source), key, job_id=job["id"])
        if return_value:
            return return_value
        return storage.s3_client().head_object(Bucket=job["s3_bucket"], Key=key)

    with patch("app.storage.upload_s3_file", side_effect=upload) as mock:
        yield mock


@contextmanager
def mock_sdk_read(*, side_effect):
    def read(job, *, target, source_size):
        chunks = []
        side_effect("read", target["object_key"], on_chunk=chunks.append, job_id=job["id"])
        content = b"".join(chunks)
        return hashlib.sha256(content).hexdigest(), len(content)

    with patch("app.storage._stream_plaintext_for_verification", side_effect=read) as mock:
        yield mock
