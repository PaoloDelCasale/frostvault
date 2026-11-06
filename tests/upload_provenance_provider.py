"""Strict persistent versioned provider: observations never manufacture versions."""
import base64
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


class VersionedProvider:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url="https://disposable.invalid", config=SimpleNamespace(region_name="test"))
        self.objects = {}
        self.markers = {}
        self.puts = self.gets = self.deletes = 0
        self.deleted = []
        self.delete_requests = 0
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

    def save(self, path):
        Path(path).write_text(json.dumps({"puts": self.puts, "objects": [{"key": k, "version": v, "bytes": base64.b64encode(data).decode(), "metadata": metadata} for (k, v), (data, metadata) in self.objects.items()]}))

    def load(self, path):
        state = json.loads(Path(path).read_text())
        self.puts = state["puts"]
        self.objects = {(r["key"],r["version"]): (base64.b64decode(r["bytes"]),r["metadata"]) for r in state["objects"]}

    def head_object(self, *, Bucket, Key, VersionId=None, **kwargs):
        if VersionId is None:
            raise AssertionError("Integrity/provenance must never HEAD latest")
        self.hook("head", key=Key, version=VersionId)
        data, metadata = self.objects[Key, VersionId]
        return {"VersionId": VersionId, "ContentLength": len(data), "Metadata": dict(metadata),
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                "ChecksumType": "FULL_OBJECT", "ETag": '"not-proof"', "StorageClass": "STANDARD"}

    def get_object(self, *, Bucket, Key, VersionId):
        self.gets += 1
        self.hook("get", key=Key, version=VersionId)
        data, _metadata = self.objects[Key, VersionId]
        return {**self.head_object(Bucket=Bucket, Key=Key, VersionId=VersionId), "Body": io.BytesIO(data)}

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return SimpleNamespace(paginate=lambda **kwargs: iter([self.list_object_versions(**kwargs)]))

    def get_object_tagging(self, **kwargs):
        return {"TagSet": []}

    def download_file(self, *, Bucket, Key, Filename, ExtraArgs, Callback, Config):
        response = self.get_object(Bucket=Bucket, Key=Key, VersionId=ExtraArgs["VersionId"])
        with response["Body"] as body:
            data = body.read()
        Path(Filename).write_bytes(data)
        Callback(len(data))

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        self.hook("list", key=Prefix)
        return {"Versions": [{"Key": k, "VersionId": v, "Size": len(self.objects[k, v][0]), "StorageClass": "STANDARD", "LastModified": datetime(2026, 11, 1, tzinfo=timezone.utc)} for k, v in sorted(self.objects) if k.startswith(Prefix)],
                "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v in sorted(self.markers) if k.startswith(Prefix)], "IsTruncated": False}

    def delete_object(self, *, Bucket, Key, VersionId):
        self.delete_requests += 1
        self.hook("delete", key=Key, version=VersionId)
        self.deletes += 1
        self.deleted.append((Key, VersionId))
        self.objects.pop((Key, VersionId), None)
        self.hook("after_delete", key=Key, version=VersionId)
        return {"VersionId": VersionId}
