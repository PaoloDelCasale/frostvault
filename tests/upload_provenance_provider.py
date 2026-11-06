"""Strict persistent versioned provider: observations never manufacture versions."""
import base64
import hashlib
import io
from types import SimpleNamespace


class VersionedProvider:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url="https://disposable.invalid", config=SimpleNamespace(region_name="test"))
        self.objects = {}
        self.markers = {}
        self.puts = self.gets = self.deletes = 0
        self.deleted = []
        self.hook = lambda stage, **kwargs: None

    def add(self, key, payload, metadata=None, *, version=None):
        version = version or f"version-{len(self.objects)+1}"
        self.objects[key, version] = (bytes(payload), dict(metadata or {}))
        return version

    def upload(self, client, *, source, bucket, key, check_active, on_progress, metadata=None, on_acknowledged=None, snapshot=None, **kwargs):
        check_active()
        self.hook("before_put", key=key, snapshot=snapshot, metadata=metadata)
        self.puts += 1
        version = self.add(key, source.read_bytes(), metadata)
        response = self.head_object(Bucket=bucket, Key=key, VersionId=version)
        self.hook("accepted", version=version)
        if on_acknowledged:
            on_acknowledged(response)
        self.hook("acknowledged", version=version)
        check_active()
        on_progress(len(self.objects[key, version][0]))
        return response

    def head_object(self, *, Bucket, Key, VersionId=None, **kwargs):
        if VersionId is None:
            raise AssertionError("Integrity/provenance must never HEAD latest")
        self.hook("head", key=Key, version=VersionId)
        data, metadata = self.objects[Key, VersionId]
        return {"VersionId": VersionId, "ContentLength": len(data), "Metadata": dict(metadata),
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                "ChecksumType": "FULL_OBJECT", "ETag": '"not-proof"', "StorageClass": "STANDARD"}

    def get_object(self, *, Bucket, Key, VersionId):
        self.hook("get", key=Key, version=VersionId)
        data, _metadata = self.objects[Key, VersionId]
        self.gets += 1
        return {**self.head_object(Bucket=Bucket, Key=Key, VersionId=VersionId), "Body": io.BytesIO(data)}

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        self.hook("list", key=Prefix)
        return {"Versions": [{"Key": k, "VersionId": v} for k, v in sorted(self.objects) if k.startswith(Prefix)],
                "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v in sorted(self.markers) if k.startswith(Prefix)], "IsTruncated": False}

    def delete_object(self, *, Bucket, Key, VersionId):
        self.hook("delete", key=Key, version=VersionId)
        self.deletes += 1
        self.deleted.append((Key, VersionId))
        self.objects.pop((Key, VersionId), None)
        return {"VersionId": VersionId}
