"""Version-aware provider stub for transfer seams (no network or catalog reads)."""
from types import SimpleNamespace


class VersionedS3Stub(SimpleNamespace):
    def __init__(self, *, head_object, delete_object=None, initial_versions=None, **kwargs):
        super().__init__(**kwargs)
        self._head = head_object
        self._delete = delete_object
        self.versions = {key: set(ids) for key, ids in (initial_versions or {}).items()}
        self.deleted = []

    def head_object(self, **kwargs):
        result = self._head(**kwargs)
        if result.get('VersionId'):
            self.versions.setdefault(kwargs['Key'], set()).add(result['VersionId'])
        return result

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
