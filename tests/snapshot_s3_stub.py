"""Version-aware provider stub for transfer seams (no network or catalog reads)."""
from types import SimpleNamespace
import io


class VersionedS3Stub(SimpleNamespace):
    def __init__(self, *, head_object, delete_object=None, initial_versions=None, version_bytes=None, **kwargs):
        super().__init__(**kwargs)
        self.version_bytes = version_bytes
        self.reads = []
        self._head = head_object
        self._delete = delete_object
        self.versions = {key: set(ids) for key, ids in (initial_versions or {}).items()}
        self.deleted = []

    def head_object(self, **kwargs):
        result = self._head(**kwargs)
        if result.get('VersionId'):
            self.versions.setdefault(kwargs['Key'], set()).add(result['VersionId'])
        return result

    def get_object(self, **kwargs):
        self.reads.append(dict(kwargs))
        if not kwargs.get('VersionId'):
            raise AssertionError('Integrity reads must pin an exact VersionId')
        if self.version_bytes is None:
            raise AssertionError('Test must supply provider version bytes')
        content = self.version_bytes
        if isinstance(content, dict):
            content = content[(kwargs['Key'], kwargs['VersionId'])]
        return {'Body': io.BytesIO(content)}

    def list_object_versions(self, **kwargs):
        prefix = kwargs.get('Prefix', '')
        return {'Versions': [
            {'Key': key, 'VersionId': version}
            for key, versions in self.versions.items() if key.startswith(prefix)
            for version in sorted(versions)
        ], 'DeleteMarkers': [], 'IsTruncated': False}

    def delete_object(self, **kwargs):
        if not kwargs.get('VersionId'):
            raise AssertionError('Snapshot cleanup must delete exact versions, never hide keys')
        self.deleted.append(dict(kwargs))
        result = self._delete(**kwargs) if self._delete else {'VersionId': kwargs['VersionId']}
        self.versions.get(kwargs['Key'], set()).discard(kwargs['VersionId'])
        return result
