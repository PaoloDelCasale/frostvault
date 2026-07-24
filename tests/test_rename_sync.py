"""Synchronize file and folder renames (issue #19).

Seams under test:
- ArchiveCatalog rename analysis and confirmation — Vault File identity and
  Path History observed through get_file_by_path / list_path_history /
  list_versions.
- Rename Job worker (process_jobs_once) — cloud copy/verify/delete-marker
  observed through Job status, Archive Version integrity/keys, and Delete
  Markers. System boundaries mocked: Rclone and S3.
- HTTP file history — continuous versions across old and new keys.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.storage import process_jobs_once
from tests.test_database import run_alembic


DIGEST_A = hashlib.sha256(b"content-a").hexdigest()
DIGEST_B = hashlib.sha256(b"content-b").hexdigest()


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _seed_vault(connection, *, vault_id: int = 2, source_root: str = "/source") -> None:
    connection.execute(
        """
        INSERT INTO users(
            id, username, display_name, password_hash, is_admin
        ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
        """
    )
    connection.execute(
        """
        INSERT INTO vaults(
            id, slug, name, source_root, s3_bucket, s3_prefix,
            rclone_remote
        ) VALUES (%s, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
        """,
        (vault_id, source_root),
    )


def _prepare_renamed_plain_file(
    root: Path,
    *,
    old_path: str,
    new_path: str,
    payload: bytes,
) -> tuple[Path, Path, str]:
    source = root / "source"
    source.mkdir()
    target = source / new_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    digest = _sha256_hex(payload)
    with SQLiteConnection(str(database_path)) as connection:
        _seed_vault(connection, source_root=str(source))
        catalog = ArchiveCatalog(connection)
        file_id = catalog.observe_local_copy(
            vault_id=2,
            path=old_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=target.stat().st_mtime_ns,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        version_id = catalog.record_archive_version(
            vault_id=2,
            path=old_path,
            object_key=f"docs/{old_path}",
            provider_version_id="old-s3-version",
            size=len(payload),
            storage_class="STANDARD",
            etag="old-etag",
            uploaded_at="2026-07-21T10:01:00+00:00",
            observed_at="2026-07-21T10:01:00+00:00",
            scan_id="2026-07-21T10:01:00+00:00",
            origin="upload",
        )
        catalog.mark_version_verified(
            version_id,
            plaintext_sha256=digest,
            verified_at="2026-07-21T10:02:00+00:00",
        )
        catalog.set_local_fingerprint(
            vault_id=2,
            path=old_path,
            plaintext_sha256=digest,
            matched_archive_version_id=version_id,
        )
        catalog.mark_local_copy_missing(
            file_id, observed_at="2026-07-21T11:00:00+00:00"
        )
        catalog.observe_local_copy(
            vault_id=2,
            path=new_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=target.stat().st_mtime_ns,
            observed_at="2026-07-21T11:00:00+00:00",
        )
        catalog.set_local_fingerprint(
            vault_id=2,
            path=new_path,
            plaintext_sha256=digest,
            matched_archive_version_id=None,
        )
        catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path=new_path,
            changed_at="2026-07-21T11:05:00+00:00",
        )
    return source, database_path, file_id


@contextmanager
def _rename_worker(
    database_path: Path,
    *,
    payload: bytes,
    old_key: str,
    new_key: str,
):
    rclone_calls: list[tuple[str, ...]] = []
    deleted_keys: list[dict[str, str]] = []
    head_calls: list[dict[str, str]] = []

    def fake_rclone(*args, **kwargs) -> None:
        command = tuple(str(arg) for arg in args if not callable(arg))
        rclone_calls.append(command)
        if command[:1] != ("copyto",) or len(command) < 3:
            return
        origin, destination = command[1], command[2]
        if ":" in origin and not Path(origin).exists():
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    def fake_head_object(**kwargs):
        head_calls.append({"Bucket": kwargs["Bucket"], "Key": kwargs["Key"]})
        if kwargs["Key"] == old_key:
            return {
                "VersionId": "old-s3-version",
                "ContentLength": len(payload),
                "StorageClass": "STANDARD",
                "ETag": '"old-etag"',
            }
        if kwargs["Key"] != new_key:
            raise RuntimeError(f"unexpected head key {kwargs['Key']}")
        return {
            "VersionId": "new-s3-version",
            "ContentLength": len(payload),
            "StorageClass": "STANDARD",
            "ETag": '"new-etag"',
        }

    def fake_delete_object(**kwargs):
        deleted_keys.append(
            {
                "Bucket": kwargs["Bucket"],
                "Key": kwargs["Key"],
                "VersionId": kwargs.get("VersionId") or "",
            }
        )
        return {"VersionId": "delete-marker-1", "DeleteMarker": True}

    database_settings = SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
    )
    worker_settings = SimpleNamespace(
        operation_concurrency=1,
        restore_poll_interval=900,
    )
    with patch("app.database.settings", database_settings):
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.run_rclone", side_effect=fake_rclone),
            patch(
                "app.storage.s3_client",
                return_value=SimpleNamespace(
                    head_object=fake_head_object,
                    delete_object=fake_delete_object,
                ),
            ),
        ):
            yield rclone_calls, deleted_keys, head_calls


class RenameMatchingTests(unittest.TestCase):
    def test_unique_digest_match_is_auto_confirmed_without_duplicate_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "rename.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                old_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/old-name.txt",
                    file_type="regular",
                    size=9,
                    mtime_ns=100,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path="reports/old-name.txt",
                    plaintext_sha256=DIGEST_A,
                    matched_archive_version_id=None,
                )
                catalog.mark_local_copy_missing(
                    old_id, observed_at="2026-07-21T11:00:00+00:00"
                )
                new_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/new-name.txt",
                    file_type="regular",
                    size=9,
                    mtime_ns=100,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path="reports/new-name.txt",
                    plaintext_sha256=DIGEST_A,
                    matched_archive_version_id=None,
                )

                candidates = catalog.list_rename_candidates(vault_id=2)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["decision"], "auto")
                self.assertEqual(candidates[0]["missing_vault_file_id"], old_id)
                self.assertEqual(candidates[0]["new_vault_file_id"], new_id)

                confirmed_id = catalog.confirm_file_rename(
                    vault_file_id=old_id,
                    new_path="reports/new-name.txt",
                    changed_at="2026-07-21T11:05:00+00:00",
                )

                renamed = catalog.get_file_by_path(2, "reports/new-name.txt")
                old_path = catalog.get_file_by_path(2, "reports/old-name.txt")
                history = catalog.list_path_history(confirmed_id)

            self.assertEqual(confirmed_id, old_id)
            self.assertEqual(renamed["id"], old_id)
            self.assertEqual(renamed["local_copy"]["presence"], "present")
            self.assertIsNone(old_path)
            self.assertEqual(
                [entry["path"] for entry in history],
                ["reports/old-name.txt", "reports/new-name.txt"],
            )

    def test_ambiguous_equal_digest_candidates_are_never_auto_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "ambiguous.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                missing_ids = []
                for path in ("a/one.txt", "a/two.txt"):
                    file_id = catalog.observe_local_copy(
                        vault_id=2,
                        path=path,
                        file_type="regular",
                        size=9,
                        mtime_ns=100,
                        observed_at="2026-07-21T10:00:00+00:00",
                    )
                    catalog.set_local_fingerprint(
                        vault_id=2,
                        path=path,
                        plaintext_sha256=DIGEST_A,
                        matched_archive_version_id=None,
                    )
                    catalog.mark_local_copy_missing(
                        file_id, observed_at="2026-07-21T11:00:00+00:00"
                    )
                    missing_ids.append(file_id)
                for path in ("b/one.txt", "b/two.txt"):
                    catalog.observe_local_copy(
                        vault_id=2,
                        path=path,
                        file_type="regular",
                        size=9,
                        mtime_ns=100,
                        observed_at="2026-07-21T11:00:00+00:00",
                    )
                    catalog.set_local_fingerprint(
                        vault_id=2,
                        path=path,
                        plaintext_sha256=DIGEST_A,
                        matched_archive_version_id=None,
                    )

                candidates = catalog.list_rename_candidates(vault_id=2)
                decisions = {candidate["decision"] for candidate in candidates}
                before_a = catalog.get_file_by_path(2, "a/one.txt")
                before_b = catalog.get_file_by_path(2, "b/one.txt")

            self.assertTrue(candidates)
            self.assertEqual(decisions, {"ambiguous"})
            self.assertNotEqual(before_a["id"], before_b["id"])
            self.assertEqual(before_a["id"], missing_ids[0])

    def test_size_and_mtime_alone_never_produce_automatic_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "metadata-only.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                old_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/old-name.txt",
                    file_type="regular",
                    size=42,
                    mtime_ns=1_750_000_000_000_000_000,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.mark_local_copy_missing(
                    old_id, observed_at="2026-07-21T11:00:00+00:00"
                )
                new_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/new-name.txt",
                    file_type="regular",
                    size=42,
                    mtime_ns=1_750_000_000_000_000_000,
                    observed_at="2026-07-21T11:00:00+00:00",
                )

                candidates = catalog.list_rename_candidates(vault_id=2)
                auto = [c for c in candidates if c["decision"] == "auto"]
                old_file = catalog.get_file_by_path(2, "reports/old-name.txt")
                new_file = catalog.get_file_by_path(2, "reports/new-name.txt")

            self.assertEqual(auto, [])
            self.assertEqual(old_file["id"], old_id)
            self.assertEqual(new_file["id"], new_id)
            self.assertNotEqual(old_id, new_id)


class RenameCloudJobTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import storage

        storage.cancelled_jobs.clear()

    def test_plain_rename_verifies_new_key_before_hiding_old_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"rename-plain"
            old_path = "reports/old-name.txt"
            new_path = "archive/new-name.txt"
            old_key = f"docs/{old_path}"
            new_key = f"docs/{new_path}"
            _source, database_path, file_id = _prepare_renamed_plain_file(
                root,
                old_path=old_path,
                new_path=new_path,
                payload=payload,
            )

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", database_settings):
                queued = queue_jobs(new_path, "rename", 2, 1)
                self.assertEqual(queued["item_count"], 1)
                with _rename_worker(
                    database_path,
                    payload=payload,
                    old_key=old_key,
                    new_key=new_key,
                ) as (rclone_calls, deleted_keys, head_calls):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, new_path)
                versions = catalog.list_versions(2, new_path)
                markers = connection.execute(
                    """
                    SELECT object_key, provider_version_id
                    FROM delete_markers
                    WHERE vault_file_id=%s
                    ORDER BY created_at
                    """,
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    (new_path,),
                ).fetchone()

            self.assertEqual(observed["id"], file_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(len(versions), 2)
            by_key = {row["object_key"]: row for row in versions}
            self.assertEqual(by_key[old_key]["integrity"], "verified")
            self.assertEqual(by_key[new_key]["integrity"], "verified")
            self.assertEqual(by_key[new_key]["plaintext_sha256"], _sha256_hex(payload))
            self.assertEqual(
                [(row["object_key"], row["provider_version_id"]) for row in markers],
                [(old_key, "delete-marker-1")],
            )
            self.assertEqual(
                deleted_keys,
                [{"Bucket": "bucket", "Key": old_key, "VersionId": ""}],
            )
            self.assertEqual([call["Key"] for call in head_calls], [new_key, old_key])
            self.assertTrue(any(call[:1] == ("copyto",) for call in rclone_calls))

    def test_failed_verification_never_hides_the_old_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"rename-mismatch"
            old_path = "old.txt"
            new_path = "new.txt"
            old_key = f"docs/{old_path}"
            new_key = f"docs/{new_path}"
            _source, database_path, file_id = _prepare_renamed_plain_file(
                root,
                old_path=old_path,
                new_path=new_path,
                payload=payload,
            )

            def fake_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                if command[:1] != ("copyto",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                if ":" in origin and not Path(origin).exists():
                    target = Path(destination)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"tampered-cloud-bytes")

            deleted: list[str] = []
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queue_jobs(new_path, "rename", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=fake_rclone),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **_: {
                                "VersionId": "new-s3-version",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            },
                            delete_object=lambda **kwargs: deleted.append(kwargs["Key"])
                            or {"VersionId": "delete-marker-1", "DeleteMarker": True},
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                versions = catalog.list_versions(2, new_path)
                markers = connection.execute(
                    "SELECT id FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    (new_path,),
                ).fetchone()

            self.assertEqual(deleted, [])
            self.assertEqual(markers, [])
            self.assertEqual(job["status"], "failed")
            by_key = {row["object_key"]: row for row in versions}
            self.assertEqual(by_key[old_key]["integrity"], "verified")
            self.assertEqual(by_key[new_key]["integrity"], "mismatch")

    def test_rename_cancellation_leaves_old_key_accessible(self) -> None:
        from app.storage import OperationCancelled, cancel_jobs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"rename-cancel"
            old_path = "old.txt"
            new_path = "new.txt"
            _source, database_path, file_id = _prepare_renamed_plain_file(
                root,
                old_path=old_path,
                new_path=new_path,
                payload=payload,
            )
            job_id_holder: dict[str, int] = {}

            def cancelling_rclone(*args, **kwargs) -> None:
                cancel_jobs([job_id_holder["id"]])
                raise OperationCancelled("Rename stopped")

            deleted: list[str] = []
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queue_jobs(new_path, "rename", 2, 1)
                with SQLiteConnection(str(database_path)) as connection:
                    job_id_holder["id"] = connection.execute(
                        "SELECT id FROM jobs WHERE path=%s",
                        (new_path,),
                    ).fetchone()["id"]
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=cancelling_rclone),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **_: {
                                "VersionId": "new-s3-version",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            },
                            delete_object=lambda **kwargs: deleted.append(kwargs["Key"])
                            or {"VersionId": "delete-marker-1", "DeleteMarker": True},
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, new_path)
                versions = catalog.list_versions(2, new_path)
                markers = connection.execute(
                    "SELECT id FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s",
                    (new_path,),
                ).fetchone()

            self.assertEqual(job["status"], "cancelled")
            self.assertEqual(deleted, [])
            self.assertEqual(markers, [])
            self.assertEqual(observed["id"], file_id)
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["object_key"], f"docs/{old_path}")
            self.assertEqual(versions[0]["integrity"], "verified")


class FolderRenameTests(unittest.TestCase):
    def test_folder_rename_updates_all_descendants_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "folder.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                ids = {}
                for relative in ("docs/a.txt", "docs/nested/b.txt", "docs/nested/c.txt"):
                    file_id = catalog.observe_local_copy(
                        vault_id=2,
                        path=relative,
                        file_type="regular",
                        size=3,
                        mtime_ns=10,
                        observed_at="2026-07-21T10:00:00+00:00",
                    )
                    catalog.set_local_fingerprint(
                        vault_id=2,
                        path=relative,
                        plaintext_sha256=_sha256_hex(relative.encode()),
                        matched_archive_version_id=None,
                    )
                    catalog.mark_local_copy_missing(
                        file_id, observed_at="2026-07-21T11:00:00+00:00"
                    )
                    ids[relative] = file_id
                    new_relative = relative.replace("docs/", "archive/", 1)
                    catalog.observe_local_copy(
                        vault_id=2,
                        path=new_relative,
                        file_type="regular",
                        size=3,
                        mtime_ns=10,
                        observed_at="2026-07-21T11:00:00+00:00",
                    )
                    catalog.set_local_fingerprint(
                        vault_id=2,
                        path=new_relative,
                        plaintext_sha256=_sha256_hex(relative.encode()),
                        matched_archive_version_id=None,
                    )

                renamed_ids = catalog.confirm_folder_rename(
                    vault_id=2,
                    old_prefix="docs",
                    new_prefix="archive",
                    changed_at="2026-07-21T11:05:00+00:00",
                )

                for old_relative, file_id in ids.items():
                    new_relative = old_relative.replace("docs/", "archive/", 1)
                    observed = catalog.get_file_by_path(2, new_relative)
                    missing = catalog.get_file_by_path(2, old_relative)
                    history = catalog.list_path_history(file_id)
                    self.assertEqual(observed["id"], file_id)
                    self.assertIsNone(missing)
                    self.assertEqual(
                        [entry["path"] for entry in history],
                        [old_relative, new_relative],
                    )
                self.assertEqual(set(renamed_ids), set(ids.values()))
                self.assertIsNone(catalog.get_file_by_path(2, "docs/a.txt"))

    def test_case_only_and_unicode_renames_preserve_identity(self) -> None:
        cases = (
            ("Reports/Photo.JPG", "reports/photo.jpg"),
            ("album/unicodé café.txt", "album/πλάνο.txt"),
        )
        for old_path, new_path in cases:
            with self.subTest(old_path=old_path, new_path=new_path):
                with tempfile.TemporaryDirectory() as directory:
                    database_path = Path(directory) / "special.db"
                    migrated = run_alembic(database_path)
                    self.assertEqual(migrated.returncode, 0, migrated.stderr)
                    digest = _sha256_hex(f"{old_path}->{new_path}".encode())
                    with SQLiteConnection(str(database_path)) as connection:
                        _seed_vault(connection)
                        catalog = ArchiveCatalog(connection)
                        file_id = catalog.observe_local_copy(
                            vault_id=2,
                            path=old_path,
                            file_type="regular",
                            size=12,
                            mtime_ns=11,
                            observed_at="2026-07-21T10:00:00+00:00",
                        )
                        catalog.set_local_fingerprint(
                            vault_id=2,
                            path=old_path,
                            plaintext_sha256=digest,
                            matched_archive_version_id=None,
                        )
                        catalog.mark_local_copy_missing(
                            file_id, observed_at="2026-07-21T11:00:00+00:00"
                        )
                        catalog.observe_local_copy(
                            vault_id=2,
                            path=new_path,
                            file_type="regular",
                            size=12,
                            mtime_ns=11,
                            observed_at="2026-07-21T11:00:00+00:00",
                        )
                        catalog.set_local_fingerprint(
                            vault_id=2,
                            path=new_path,
                            plaintext_sha256=digest,
                            matched_archive_version_id=None,
                        )
                        confirmed = catalog.confirm_file_rename(
                            vault_file_id=file_id,
                            new_path=new_path,
                            changed_at="2026-07-21T11:05:00+00:00",
                        )
                        history = [
                            entry["path"]
                            for entry in catalog.list_path_history(confirmed)
                        ]
                        observed = catalog.get_file_by_path(2, new_path)
                    self.assertEqual(confirmed, file_id)
                    self.assertEqual(observed["id"], file_id)
                    self.assertEqual(history, [old_path, new_path])


class CryptRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import storage

        storage.cancelled_jobs.clear()

    def test_crypt_rename_verifies_new_key_before_hiding_old_key(self) -> None:
        from app.services.rclone_runtime import RuntimeRcloneConfig

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            payload = b"secret-rename"
            old_path = "old-secret.txt"
            new_path = "new-secret.txt"
            old_encrypted = "nq/old-enc"
            new_encrypted = "nq/new-enc"
            old_key = f"docs/{old_encrypted}"
            new_key = f"docs/{new_encrypted}"
            target = source / new_path
            target.write_bytes(payload)
            digest = _sha256_hex(payload)
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote, encryption_mode,
                        crypt_password_ciphertext, crypt_password2_ciphertext,
                        recovery_custody_confirmed_at
                    ) VALUES (
                        2, 'secret', 'Secret', %s, 'bucket', 'docs', 'base',
                        'crypt', 'cipher-a', 'cipher-b',
                        '2026-07-21T09:00:00+00:00'
                    )
                    """,
                    (str(source),),
                )
                catalog = ArchiveCatalog(connection)
                file_id = catalog.observe_local_copy(
                    vault_id=2,
                    path=old_path,
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                version_id = catalog.record_archive_version(
                    vault_id=2,
                    path=old_path,
                    object_key=old_key,
                    provider_version_id="crypt-old",
                    size=len(payload),
                    storage_class="STANDARD",
                    etag="old",
                    uploaded_at="2026-07-21T10:01:00+00:00",
                    observed_at="2026-07-21T10:01:00+00:00",
                    scan_id="2026-07-21T10:01:00+00:00",
                    origin="upload",
                )
                catalog.mark_version_verified(
                    version_id,
                    plaintext_sha256=digest,
                    verified_at="2026-07-21T10:02:00+00:00",
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path=old_path,
                    plaintext_sha256=digest,
                    matched_archive_version_id=version_id,
                )
                catalog.mark_local_copy_missing(
                    file_id, observed_at="2026-07-21T11:00:00+00:00"
                )
                catalog.observe_local_copy(
                    vault_id=2,
                    path=new_path,
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path=new_path,
                    plaintext_sha256=digest,
                    matched_archive_version_id=None,
                )
                catalog.confirm_file_rename(
                    vault_file_id=file_id,
                    new_path=new_path,
                    changed_at="2026-07-21T11:05:00+00:00",
                )

            fake_config = RuntimeRcloneConfig(
                path=root / "fake.rclone.conf",
                remote_name="vault",
                config_text="[vault]\ntype = crypt\n",
                secrets=None,
            )
            fake_config.path.write_text(fake_config.config_text, encoding="utf-8")

            @contextmanager
            def fake_vault_rclone_config(_vault):
                yield fake_config

            def fake_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                if command[:1] != ("copyto",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                if ":" in origin and not Path(origin).exists():
                    Path(destination).parent.mkdir(parents=True, exist_ok=True)
                    Path(destination).write_bytes(payload)

            deleted: list[str] = []
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", database_settings):
                queue_jobs(new_path, "rename", 2, 1)
                with (
                    patch(
                        "app.storage.settings",
                        SimpleNamespace(
                            operation_concurrency=1,
                            restore_poll_interval=900,
                        ),
                    ),
                    patch("app.storage.validate_cloud_vault"),
                    patch(
                        "app.storage.vault_rclone_config",
                        side_effect=fake_vault_rclone_config,
                    ),
                    patch(
                        "app.storage.encode_object_relative_path",
                        side_effect=lambda _runtime, path: (
                            new_encrypted if path == new_path else old_encrypted
                        ),
                    ),
                    patch(
                        "app.storage.secrets_for_vault",
                        return_value=SimpleNamespace(password="p", password2="p2"),
                    ),
                    patch("app.storage.run_rclone", side_effect=fake_rclone),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **kwargs: {
                                "VersionId": (
                                    "crypt-old"
                                    if kwargs["Key"] == old_key
                                    else "crypt-new"
                                ),
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            },
                            delete_object=lambda **kwargs: deleted.append(kwargs["Key"])
                            or {
                                "VersionId": "crypt-delete-marker",
                                "DeleteMarker": True,
                            },
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, new_path)
                versions = catalog.list_versions(2, new_path)
                markers = connection.execute(
                    """
                    SELECT object_key, provider_version_id
                    FROM delete_markers WHERE vault_file_id=%s
                    """,
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s",
                    (new_path,),
                ).fetchone()

            self.assertEqual(observed["id"], file_id)
            self.assertEqual(job["status"], "completed")
            by_key = {row["object_key"]: row for row in versions}
            self.assertEqual(by_key[old_key]["integrity"], "verified")
            self.assertEqual(by_key[new_key]["integrity"], "verified")
            self.assertNotIn(new_path, new_key)
            self.assertEqual(deleted, [old_key])
            self.assertEqual(
                [(row["object_key"], row["provider_version_id"]) for row in markers],
                [(old_key, "crypt-delete-marker")],
            )


class RenameRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import storage

        storage.cancelled_jobs.clear()

    def test_interrupted_cleaning_rename_resumes_without_losing_old_key(self) -> None:
        from app.storage import reconcile_interrupted_jobs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"rename-resume"
            old_path = "old.txt"
            new_path = "new.txt"
            old_key = f"docs/{old_path}"
            new_key = f"docs/{new_path}"
            _source, database_path, file_id = _prepare_renamed_plain_file(
                root,
                old_path=old_path,
                new_path=new_path,
                payload=payload,
            )
            digest = _sha256_hex(payload)
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                new_version_id = catalog.record_archive_version(
                    vault_id=2,
                    path=new_path,
                    object_key=new_key,
                    provider_version_id="new-s3-version",
                    size=len(payload),
                    storage_class="STANDARD",
                    etag="new-etag",
                    uploaded_at="2026-07-21T11:10:00+00:00",
                    observed_at="2026-07-21T11:10:00+00:00",
                    scan_id="2026-07-21T11:10:00+00:00",
                    origin="upload",
                )
                catalog.mark_version_verified(
                    new_version_id,
                    plaintext_sha256=digest,
                    verified_at="2026-07-21T11:11:00+00:00",
                )
                old_version = connection.execute(
                    """
                    SELECT id FROM archive_versions
                    WHERE vault_file_id=%s AND object_key=%s
                    """,
                    (file_id, old_key),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, archive_version_id, path,
                        action, status, requested_by, requested_at, updated_at,
                        group_id, group_path, total_bytes, transferred_bytes,
                        message
                    ) VALUES (
                        2, %s, %s, %s, 'rename', 'cleaning', 1,
                        '2026-07-21T11:12:00+00:00', '2026-07-21T11:12:00+00:00',
                        'group-1', %s, %s, 0, 'Hiding the previous cloud key'
                    )
                    """,
                    (
                        file_id,
                        old_version["id"],
                        new_path,
                        new_path,
                        len(payload),
                    ),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            deleted: list[str] = []
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()
                self.assertEqual(summary["requeued"], 1)
                with (
                    patch(
                        "app.storage.settings",
                        SimpleNamespace(
                            operation_concurrency=1,
                            restore_poll_interval=900,
                        ),
                    ),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone"),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **kwargs: {
                                "VersionId": "old-s3-version",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"old-etag"',
                            },
                            delete_object=lambda **kwargs: deleted.append(kwargs["Key"])
                            or {
                                "VersionId": "delete-marker-resume",
                                "DeleteMarker": True,
                            },
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                versions = ArchiveCatalog(connection).list_versions(2, new_path)
                markers = connection.execute(
                    """
                    SELECT object_key, provider_version_id
                    FROM delete_markers WHERE vault_file_id=%s
                    """,
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s",
                    (new_path,),
                ).fetchone()
                version_count = connection.execute(
                    "SELECT COUNT(*) AS n FROM archive_versions WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchone()["n"]

            self.assertEqual(job["status"], "completed")
            self.assertEqual(deleted, [old_key])
            self.assertEqual(version_count, 2)
            self.assertEqual(len(versions), 2)
            self.assertEqual(
                [(row["object_key"], row["provider_version_id"]) for row in markers],
                [(old_key, "delete-marker-resume")],
            )


class FileHistoryApiTests(unittest.TestCase):
    def test_file_history_returns_continuous_versions_across_keys(self) -> None:
        from app.main import file_history

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"history"
            old_path = "old.txt"
            new_path = "new.txt"
            _source, database_path, file_id = _prepare_renamed_plain_file(
                root,
                old_path=old_path,
                new_path=new_path,
                payload=payload,
            )
            digest = _sha256_hex(payload)
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                new_version_id = catalog.record_archive_version(
                    vault_id=2,
                    path=new_path,
                    object_key=f"docs/{new_path}",
                    provider_version_id="new-s3-version",
                    size=len(payload),
                    storage_class="STANDARD",
                    etag="new-etag",
                    uploaded_at="2026-07-21T11:10:00+00:00",
                    observed_at="2026-07-21T11:10:00+00:00",
                    scan_id="2026-07-21T11:10:00+00:00",
                    origin="upload",
                )
                catalog.mark_version_verified(
                    new_version_id,
                    plaintext_sha256=digest,
                    verified_at="2026-07-21T11:11:00+00:00",
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", database_settings):
                body = file_history(
                    path=new_path,
                    vault={"id": 2, "role": "owner", "name": "Docs"},
                )

            self.assertEqual(body["vault_file_id"], file_id)
            self.assertEqual(
                [entry["path"] for entry in body["path_history"]],
                [old_path, new_path],
            )
            keys = [version["object_key"] for version in body["versions"]]
            self.assertEqual(keys, [f"docs/{new_path}", f"docs/{old_path}"])


if __name__ == "__main__":
    unittest.main()
