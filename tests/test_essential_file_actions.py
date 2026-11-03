"""#417: only safe automatic renames may clean up verified cloud content."""
from __future__ import annotations

import tempfile
import io
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog, VaultFileNotFound
from app.database import SQLiteConnection
from app.main import app
from app.storage import apply_auto_renames, process_jobs_once
from tests.test_rename_sync import _prepare_renamed_plain_file, _sha256_hex


PAYLOAD = b"the only protected photo"
OLD = "docs/old/photo.jpg"
NEW = "docs/new/photo.jpg"


class _RenameCloud:
    """Versioned provider fake: deletions assert durable verification ordering."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.versions = {OLD: {"old-s3-version"}, NEW: set()}
        self.deleted = []
        self.downloaded = []
        self.download_payload = PAYLOAD
        self.omit_destination = False
        self.fail_listing = False
        self.fail_delete = False

    def head_object(self, *, Bucket, Key, VersionId=None):
        version = VersionId or ("old-s3-version" if Key == OLD else "new-s3-version")
        if version not in self.versions.get(Key, set()):
            raise RuntimeError("missing exact provider version")
        return {"VersionId": version, "ContentLength": len(PAYLOAD), "StorageClass": "STANDARD"}

    def upload(self, *args, **kwargs):
        self.versions[NEW].add("new-s3-version")

    def get_object(self, *, Bucket, Key, VersionId):
        assert VersionId in self.versions[Key]
        return {"Body": io.BytesIO(PAYLOAD)}

    def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None, Config=None):
        assert Key == NEW and ExtraArgs["VersionId"] == "new-s3-version"
        self.downloaded.append((Key, ExtraArgs["VersionId"]))
        Path(Filename).write_bytes(self.download_payload)
        if Callback:
            Callback(len(self.download_payload))

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        if self.fail_listing:
            raise RuntimeError("provider listing failed")
        versions = self.versions.get(Prefix, set())
        if Prefix == NEW and self.omit_destination:
            versions = set()
        return {"Versions": [{"Key": Prefix, "VersionId": v} for v in versions], "DeleteMarkers": [], "IsTruncated": False}

    def delete_object(self, *, Bucket, Key, VersionId):
        # No unversioned hide is permitted for a Current Snapshot rename.
        with SQLiteConnection(str(self.database_path)) as connection:
            destination = connection.execute(
                "SELECT integrity, plaintext_sha256 FROM archive_versions WHERE object_key=%s AND provider_version_id=%s",
                (NEW, "new-s3-version"),
            ).fetchone()
        assert destination["integrity"] == "verified"
        assert destination["plaintext_sha256"] == _sha256_hex(PAYLOAD)
        assert self.downloaded == [(NEW, "new-s3-version")]
        if self.fail_delete:
            raise RuntimeError("deletion interrupted")
        self.deleted.append((Key, VersionId))
        self.versions[Key].discard(VersionId)
        return {"VersionId": VersionId}


@contextmanager
def _worker(database_path: Path, cloud: _RenameCloud):
    with (
        patch("app.database.settings", SimpleNamespace(db_backend="sqlite", sqlite_path=str(database_path))),
        patch("app.storage.settings", SimpleNamespace(operation_concurrency=1, restore_poll_interval=900)),
        patch("app.storage.validate_cloud_vault"),
        patch("app.storage.s3_client", return_value=cloud),
        patch("app.storage.run_rclone", side_effect=cloud.upload),
        patch("app.storage.run_rclone_stream", side_effect=lambda *args, **kwargs: (kwargs["on_chunk"](PAYLOAD), len(PAYLOAD))[1]),
    ):
        yield


class EssentialFileActionTests(unittest.TestCase):
    def _prepare(self, root):
        source, database_path, file_id = _prepare_renamed_plain_file(
            root, old_path="old/photo.jpg", new_path="new/photo.jpg", payload=PAYLOAD, confirm=False,
        )
        with SQLiteConnection(str(database_path)) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
        return source, database_path, file_id, vault

    def test_safe_rename_verifies_new_path_before_deleting_old_snapshot_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            source, database_path, file_id, vault = self._prepare(Path(directory))
            cloud = _RenameCloud(database_path)
            with _worker(database_path, cloud):
                summary = apply_auto_renames(vault)
                self.assertEqual(summary["confirmed"], 1)
                self.assertEqual(summary["queued"], 1)
                process_jobs_once()
            self.assertEqual(cloud.deleted, [(OLD, "old-s3-version")])
            self.assertEqual(cloud.versions, {OLD: set(), NEW: {"new-s3-version"}})
            self.assertEqual((source / "new/photo.jpg").read_bytes(), PAYLOAD)
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                self.assertEqual(catalog.get_file_by_path(2, "new/photo.jpg")["id"], file_id)
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM vault_files WHERE status='active'").fetchone()["n"], 1)
                versions = catalog.list_versions(2, "new/photo.jpg")
                self.assertEqual([v["provider_version_id"] for v in versions if v["recoverable"]], ["new-s3-version"])
                self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"], "completed")

    def test_ambiguous_match_is_a_new_upload_and_never_deletes_cloud_content(self):
        for extra_present in (False, True):
            with self.subTest(extra_present=extra_present), tempfile.TemporaryDirectory() as directory:
                source, database_path, file_id, vault = self._prepare(Path(directory))
                with SQLiteConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    path = "other/photo.jpg"
                    extra_id = catalog.observe_local_copy(vault_id=2, path=path, file_type="regular", size=len(PAYLOAD), mtime_ns=1, observed_at="2026-07-21T11:00:00+00:00")
                    catalog.set_local_fingerprint(vault_id=2, path=path, plaintext_sha256=_sha256_hex(PAYLOAD), matched_archive_version_id=None)
                    if not extra_present:
                        catalog.mark_local_copy_missing(extra_id, observed_at="2026-07-21T11:00:00+00:00")
                    with self.assertRaises(VaultFileNotFound):
                        catalog.confirm_file_rename(vault_file_id=file_id, new_path="new/photo.jpg", changed_at="2026-07-21T11:05:00+00:00", vault_id=2)
                cloud = _RenameCloud(database_path)
                with _worker(database_path, cloud):
                    summary = apply_auto_renames(vault)
                    with SQLiteConnection(str(database_path)) as connection:
                        jobs, _total, _eligible = ArchiveCatalog(connection).queue_jobs(
                            vault_id=2, path="new/photo.jpg", action="upload",
                            requested_by=1, requested_at="2026-07-21T11:05:00+00:00",
                            group_id="ambiguous-new-upload", is_directory=False,
                        )
                        self.assertEqual(len(jobs), 1)
                    process_jobs_once()
                self.assertEqual(summary["confirmed"], 0)
                self.assertEqual(cloud.deleted, [])
                self.assertEqual(cloud.downloaded, [])
                with SQLiteConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    current = catalog.get_file_by_path(2, "new/photo.jpg")
                    self.assertNotEqual(current["id"], file_id)
                    self.assertEqual(len(catalog.list_path_history(current["id"], vault_id=2)), 1)
                    rows = catalog.list_file_rows(2)
                    self.assertFalse(next(row for row in rows if row["path"] == "new/photo.jpg")["upload_eligible"])
                    self.assertEqual(connection.execute("SELECT status FROM jobs WHERE action='upload'").fetchone()["status"], "completed")
                    self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM jobs WHERE action='rename'").fetchone()["n"], 0)
                    self.assertEqual(catalog.list_versions(2, "old/photo.jpg")[0]["availability"], "available")

    def test_failed_or_uncertain_rename_never_deletes_the_only_copy(self):
        for failure in ("mismatch", "missing_keeper", "stale_old_key", "stale_new_key", "listing_error"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                _source, database_path, _file_id, vault = self._prepare(Path(directory))
                cloud = _RenameCloud(database_path)
                if failure == "mismatch": cloud.download_payload = b"corrupt destination"
                if failure == "missing_keeper": cloud.omit_destination = True
                if failure == "stale_old_key": cloud.versions[OLD] = {"uncatalogued-only-copy"}
                if failure == "stale_new_key": cloud.versions[NEW] = {"uncatalogued-only-copy"}
                if failure == "listing_error": cloud.fail_listing = True
                with _worker(database_path, cloud):
                    apply_auto_renames(vault)
                    process_jobs_once()
                self.assertEqual(cloud.deleted, [])
                self.assertTrue(cloud.versions[OLD])
                with SQLiteConnection(str(database_path)) as connection:
                    self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"], "failed")
                    self.assertEqual(connection.execute("SELECT availability FROM archive_versions WHERE object_key=%s", (OLD,)).fetchone()["availability"], "available")

    def test_snapshot_retry_with_existing_marker_does_not_skip_old_version_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, database_path, file_id, vault = self._prepare(Path(directory))
            cloud = _RenameCloud(database_path)
            cloud.fail_delete = True
            with _worker(database_path, cloud):
                apply_auto_renames(vault)
                process_jobs_once()
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                catalog.record_delete_marker(vault_id=2, path="new/photo.jpg", object_key=OLD, provider_version_id="legacy-marker", created_at="2026-07-21", observed_at="2026-07-21")
                connection.execute("UPDATE jobs SET status='queued', claim_token=NULL, claim_expires_at=NULL")
            cloud.fail_delete = False
            with _worker(database_path, cloud):
                process_jobs_once()
            self.assertEqual(cloud.deleted, [(OLD, "old-s3-version")])
            with SQLiteConnection(str(database_path)) as connection:
                self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"], "completed")
                self.assertEqual(len([v for v in ArchiveCatalog(connection).list_versions(2, "new/photo.jpg") if v["recoverable"]]), 1)

    def test_archived_or_busy_destination_is_not_consumed_as_a_provisional_file(self):
        for state in ("archived", "uploading"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                _source, database_path, _file_id, vault = self._prepare(Path(directory))
                with SQLiteConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    destination_id = catalog.get_file_by_path(2, "new/photo.jpg")["id"]
                    if state == "archived":
                        catalog.record_archive_version(
                            vault_id=2, path="new/photo.jpg", object_key=NEW,
                            provider_version_id="independent-copy", size=len(PAYLOAD),
                            storage_class="STANDARD", etag="independent",
                            uploaded_at="2026-07-21", observed_at="2026-07-21", scan_id="test",
                        )
                    else:
                        catalog.queue_jobs(
                            vault_id=2, path="new/photo.jpg", action="upload", requested_by=1,
                            requested_at="2026-07-21", group_id="busy-upload", is_directory=False,
                        )
                cloud = _RenameCloud(database_path)
                with _worker(database_path, cloud):
                    summary = apply_auto_renames(vault)
                self.assertEqual(summary["confirmed"], 0)
                self.assertEqual(cloud.deleted, [])
                with SQLiteConnection(str(database_path)) as connection:
                    self.assertEqual(ArchiveCatalog(connection).get_file_by_path(2, "new/photo.jpg")["id"], destination_id)

    def test_confirmation_rechecks_only_its_fingerprint_group(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, database_path, file_id, _vault = self._prepare(Path(directory))
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                with patch.object(catalog, "list_rename_candidates", wraps=catalog.list_rename_candidates) as recheck:
                    catalog.confirm_file_rename(
                        vault_file_id=file_id, new_path="new/photo.jpg",
                        changed_at="2026-07-21T11:05:00+00:00", vault_id=2,
                    )
                recheck.assert_called_once_with(2, digest=_sha256_hex(PAYLOAD))

    def test_removed_routes_and_contracts_are_not_exposed(self):
        paths = app.openapi()["paths"]
        for path in ("/api/cloud-archive", "/api/rename-candidates", "/api/confirm-rename", "/api/confirm-folder-rename", "/api/file-history"):
            self.assertNotIn(path, paths)
        schemas = app.openapi()["components"]["schemas"]
        for schema in ("CloudArchiveRequest", "ConfirmRenameAction", "ConfirmFolderRenameAction", "FileHistoryResponse", "PathHistoryEntry"):
            self.assertNotIn(schema, schemas)
