"""In-memory S3 bucket-configuration API for bucket provisioning tests.

Answers like S3 does for the calls ``app.services.bucket_provisioning`` makes:
the same error codes for absent configuration, ``BucketAlreadyOwnedByYou``
for a repeated creation and ``BucketAlreadyExists`` for a foreign name. No
network, no credentials. ``fail`` makes one operation raise a ClientError.
"""
from __future__ import annotations

import copy
from typing import Any

from botocore.exceptions import ClientError


def client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"provider detail for {code}"},
                        "ResponseMetadata": {"HTTPStatusCode": 400}}, operation)


class FakeBucketS3:
    def __init__(self, *, foreign: set[str] | None = None):
        self.buckets: dict[str, dict[str, Any]] = {}
        self.foreign = set(foreign or ())
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, str] = {}

    def _record(self, operation: str, bucket: str) -> None:
        self.calls.append((operation, bucket))
        code = self.fail.pop(operation, None)
        if code:
            raise client_error(code, operation)

    def _bucket(self, operation: str, name: str) -> dict[str, Any]:
        self._record(operation, name)
        if name not in self.buckets:
            raise client_error("NoSuchBucket", operation)
        return self.buckets[name]

    def _setting(self, operation: str, name: str, key: str, missing: str) -> Any:
        value = self._bucket(operation, name).get(key)
        if value is None:
            raise client_error(missing, operation)
        return copy.deepcopy(value)

    def mutating_calls(self) -> list[str]:
        return [name for name, _ in self.calls
                if name.startswith(("create", "put", "delete"))]

    # -- buckets ---------------------------------------------------------
    def create_bucket(self, Bucket: str, CreateBucketConfiguration: dict | None = None,
                      ObjectLockEnabledForBucket: bool = False) -> dict:
        self._record("create_bucket", Bucket)
        if Bucket in self.foreign:
            raise client_error("BucketAlreadyExists", "CreateBucket")
        if Bucket in self.buckets:
            raise client_error("BucketAlreadyOwnedByYou", "CreateBucket")
        region = (CreateBucketConfiguration or {}).get("LocationConstraint")
        self.buckets[Bucket] = {"region": region}
        if ObjectLockEnabledForBucket:
            self.buckets[Bucket]["versioning"] = "Enabled"
            self.buckets[Bucket]["lock"] = {"ObjectLockEnabled": "Enabled"}
        return {}

    def head_bucket(self, Bucket: str) -> dict:
        if Bucket in self.foreign:
            self._record("head_bucket", Bucket)
            raise client_error("403", "HeadBucket")
        self._bucket("head_bucket", Bucket)
        return {}

    def get_bucket_location(self, Bucket: str) -> dict:
        return {"LocationConstraint": self._bucket("get_bucket_location", Bucket)["region"]}

    # -- configuration ---------------------------------------------------
    def put_public_access_block(self, Bucket: str, PublicAccessBlockConfiguration: dict) -> dict:
        self._bucket("put_public_access_block", Bucket)["bpa"] = dict(PublicAccessBlockConfiguration)
        return {}

    def get_public_access_block(self, Bucket: str) -> dict:
        return {"PublicAccessBlockConfiguration": self._setting(
            "get_public_access_block", Bucket, "bpa", "NoSuchPublicAccessBlockConfiguration")}

    def put_bucket_versioning(self, Bucket: str, VersioningConfiguration: dict) -> dict:
        self._bucket("put_bucket_versioning", Bucket)["versioning"] = VersioningConfiguration["Status"]
        return {}

    def get_bucket_versioning(self, Bucket: str) -> dict:
        status = self._bucket("get_bucket_versioning", Bucket).get("versioning")
        return {"Status": status} if status else {}

    def put_bucket_encryption(self, Bucket: str, ServerSideEncryptionConfiguration: dict) -> dict:
        self._bucket("put_bucket_encryption", Bucket)["sse"] = copy.deepcopy(
            ServerSideEncryptionConfiguration)
        return {}

    def get_bucket_encryption(self, Bucket: str) -> dict:
        return {"ServerSideEncryptionConfiguration": self._setting(
            "get_bucket_encryption", Bucket, "sse",
            "ServerSideEncryptionConfigurationNotFoundError")}

    def put_bucket_lifecycle_configuration(
            self, Bucket: str, LifecycleConfiguration: dict,
            TransitionDefaultMinimumObjectSize: str | None = None) -> dict:
        bucket = self._bucket("put_bucket_lifecycle_configuration", Bucket)
        bucket["lifecycle"] = copy.deepcopy(LifecycleConfiguration["Rules"])
        # S3 stores its default when the request names no minimum size.
        bucket["lifecycle_minimum_size"] = (TransitionDefaultMinimumObjectSize
                                            or "all_storage_classes_128K")
        return {}

    def delete_bucket_lifecycle(self, Bucket: str) -> dict:
        self._bucket("delete_bucket_lifecycle", Bucket).pop("lifecycle", None)
        return {}

    def get_bucket_lifecycle_configuration(self, Bucket: str) -> dict:
        rules = self._setting("get_bucket_lifecycle_configuration", Bucket,
                              "lifecycle", "NoSuchLifecycleConfiguration")
        return {"Rules": rules, "TransitionDefaultMinimumObjectSize":
                self.buckets[Bucket].get("lifecycle_minimum_size", "all_storage_classes_128K")}

    def put_bucket_tagging(self, Bucket: str, Tagging: dict) -> dict:
        self._bucket("put_bucket_tagging", Bucket)["tags"] = copy.deepcopy(Tagging["TagSet"])
        return {}

    def get_bucket_tagging(self, Bucket: str) -> dict:
        return {"TagSet": self._setting("get_bucket_tagging", Bucket, "tags", "NoSuchTagSet")}

    def put_object_lock_configuration(self, Bucket: str, ObjectLockConfiguration: dict) -> dict:
        self._bucket("put_object_lock_configuration", Bucket)["lock"] = copy.deepcopy(
            ObjectLockConfiguration)
        return {}

    def put_bucket_ownership_controls(self, Bucket: str, OwnershipControls: dict) -> dict:
        self._bucket("put_bucket_ownership_controls", Bucket)["ownership"] = copy.deepcopy(
            OwnershipControls)
        return {}

    def get_bucket_ownership_controls(self, Bucket: str) -> dict:
        return {"OwnershipControls": self._setting(
            "get_bucket_ownership_controls", Bucket, "ownership",
            "OwnershipControlsNotFoundError")}

    def get_object_lock_configuration(self, Bucket: str) -> dict:
        return {"ObjectLockConfiguration": self._setting(
            "get_object_lock_configuration", Bucket, "lock",
            "ObjectLockConfigurationNotFoundError")}


def fake_vault_buckets(test: Any, *, foreign: set[str] | None = None) -> FakeBucketS3:
    """Create every Vault Bucket of ``test`` in one in-memory S3 (#735).

    Patches the client ``vault_buckets`` opens by default; the patch ends
    with the test.
    """
    from unittest.mock import patch

    fake = FakeBucketS3(foreign=foreign)
    patcher = patch("app.services.vault_buckets._default_client", return_value=fake)
    patcher.start()
    test.addCleanup(patcher.stop)
    return fake
