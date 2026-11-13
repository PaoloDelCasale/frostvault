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
        # User metadata each uploaded version was written with (the upload
        # attempt's token among it), echoed by HEAD like a real provider.
        self.metadata = {}

    def remember_upload(self, key, version_id, metadata):
        if version_id:
            self.metadata[(key, version_id)] = dict(metadata or {})

    def head_object(self, **kwargs):
        result = self._head(**kwargs)
        if result.get('VersionId'):
            self.versions.setdefault(kwargs['Key'], set()).add(result['VersionId'])
            stored = self.metadata.get((kwargs['Key'], result['VersionId']))
            if stored is not None and 'Metadata' not in result:
                result = {**result, 'Metadata': dict(stored)}
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
        return {'Body': io.BytesIO(content), 'VersionId': kwargs['VersionId']}

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
