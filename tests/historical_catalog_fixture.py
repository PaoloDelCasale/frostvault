"""SQL seed builder for migration contracts on the pre-0075 catalog.

This is NOT a catalog implementation or migration compatibility path. It writes
only explicit synthetic fixtures to disposable databases. Historical ordinal
columns must never leak back into app runtime to make old tests pass.
"""
from uuid import uuid4


class HistoricalCatalogFixture:
    def __init__(self, connection):
        self.connection = connection
        columns = {row["column_name"] for row in connection.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='archive_versions'
        """).fetchall()}
        if "version_number" not in columns:
            raise ValueError("HistoricalCatalogFixture requires the pre-0075 schema")

    def _file_id(self, vault_id, path, stamp):
        row = self.connection.execute("""
            SELECT vault_file_id FROM file_paths
            WHERE vault_id=%s AND path=%s AND valid_to IS NULL
        """, (vault_id, path)).fetchone()
        if row:
            return row["vault_file_id"]
        file_id = str(uuid4())
        self.connection.execute("""
            INSERT INTO vault_files(id,vault_id,created_at) VALUES (%s,%s,%s)
        """, (file_id, vault_id, stamp))
        self.connection.execute("""
            INSERT INTO file_paths(vault_file_id,vault_id,path,valid_from)
            VALUES (%s,%s,%s,%s)
        """, (file_id, vault_id, path, stamp))
        return file_id

    def observe_local_copy(self, *, vault_id, path, file_type, size, mtime_ns, observed_at):
        file_id = self._file_id(vault_id, path, observed_at)
        self.connection.execute("""
            INSERT INTO local_copies(vault_file_id,presence,file_type,size,mtime_ns,
                                     last_seen_at,observed_at)
            VALUES (%s,'present',%s,%s,%s,%s,%s)
            ON CONFLICT (vault_file_id) DO UPDATE SET presence='present',
                file_type=excluded.file_type,size=excluded.size,mtime_ns=excluded.mtime_ns,
                last_seen_at=excluded.last_seen_at,observed_at=excluded.observed_at
        """, (file_id, file_type, size, mtime_ns, observed_at, observed_at))
        return file_id

    def record_archive_version(self, *, vault_id, path, object_key, provider_version_id,
                               size, storage_class, etag, uploaded_at, observed_at,
                               scan_id, origin="discovered"):
        file_id = self._file_id(vault_id, path, observed_at)
        number = self.connection.execute("""
            SELECT coalesce(max(version_number),0)+1 AS n FROM archive_versions
            WHERE vault_file_id=%s
        """, (file_id,)).fetchone()["n"]
        version_id = str(uuid4())
        self.connection.execute("""
            INSERT INTO archive_versions(id,vault_file_id,vault_id,version_number,
                object_key,provider_version_id,size,storage_class,etag,uploaded_at,
                discovered_at,origin,availability,availability_checked_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'available',%s)
        """, (version_id, file_id, vault_id, number, object_key, provider_version_id,
              size, storage_class, etag, uploaded_at, observed_at, origin, scan_id))
        return version_id

    def mark_version_verified(self, version_id, *, plaintext_sha256, verified_at):
        self.connection.execute("""
            UPDATE archive_versions SET integrity='verified',plaintext_sha256=%s,
                verified_at=%s WHERE id=%s
        """, (plaintext_sha256, verified_at, version_id))

    def mark_local_copy_missing(self, file_id, *, observed_at):
        self.connection.execute("""
            UPDATE local_copies SET presence='missing',observed_at=%s
            WHERE vault_file_id=%s
        """, (observed_at, file_id))

    def set_local_fingerprint(self, *, vault_id, path, plaintext_sha256,
                              matched_archive_version_id):
        self.connection.execute("""
            UPDATE local_copies SET plaintext_sha256=%s,matched_archive_version_id=%s
            WHERE vault_file_id=(SELECT vault_file_id FROM file_paths
                WHERE vault_id=%s AND path=%s AND valid_to IS NULL)
        """, (plaintext_sha256, matched_archive_version_id, vault_id, path))
