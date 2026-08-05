"""Digest-verified recovery of an exact Archive Version (issue #4).

Seams under test:
- Recovery Job worker (`process_recover` / `process_jobs_once`) observed through
  Job status and Local Copy fingerprint via ArchiveCatalog.
- Exact-version download adapter (`download_exact_version_plaintext`): must pin
  S3 VersionId; path-only current-object download is forbidden. Crypt recover
  must address rclone from Archive Version ``object_key``, not solely
  ``job['path']`` (BUG-013 / REQ-025).
- System boundaries mocked: S3 GetObject/HeadObject and Rclone.

BUG-012 (REQ-024) uses the same recovery worker seam: catalog absence plus a
real on-disk destination must fail closed without overwrite.

BUG-018 (REQ-030) uses the public cancel seam ``cancel_job_group``: after a
recover Job has persisted Glacier ``restore_state`` / expiry (RestoreObject
accepted and non-cancellable), cancel must leave that polling context intact
while still cancelling the Job.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import cancel_job_group, queue_jobs
from app.services.rclone_runtime import RuntimeRcloneConfig
from app.storage import (
    download_exact_version_plaintext,
    process_jobs_once,
    reconcile_interrupted_jobs,
)
from tests.test_database import run_alembic


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        data = self._payload
        for index in range(0, len(data), chunk_size):
            yield data[index : index + chunk_size]

    def read(self) -> bytes:
        return self._payload


def _attach_download_file(client: Mock) -> None:
    """Make ``client.download_file`` write bytes via ``client.get_object``.

    Production recovery uses boto3 TransferManager (``download_file``) for
    plain vaults. Unit tests still stub ``get_object``; this adapter keeps
    VersionId assertions on the GetObject kwargs while exercising the new path.
    """

    def download_file(
        Bucket: str,
        Key: str,
        Filename: str,
        ExtraArgs: dict | None = None,
        Callback=None,
        Config=None,
    ) -> None:
        kwargs: dict = {"Bucket": Bucket, "Key": Key}
        if ExtraArgs:
            kwargs.update(ExtraArgs)
        response = client.get_object(**kwargs)
        body = response["Body"]
        with open(Filename, "wb") as destination:
            chunks = getattr(body, "iter_chunks", None)
            if callable(chunks):
                for chunk in chunks():
                    destination.write(chunk)
                    if Callback is not None:
                        Callback(len(chunk))
            else:
                payload = body.read()
                destination.write(payload)
                if Callback is not None:
                    Callback(len(payload))

    client.download_file = Mock(side_effect=download_file)


def _prepare_cloud_only_version(
    root: Path,
    *,
    relative_path: str,
    payload: bytes,
    storage_class: str = "STANDARD",
    provider_version_id: str = "s3-version-1",
    object_key: str | None = None,
) -> tuple[Path, Path, str]:
    source = root / "source"
    source.mkdir()
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    digest = _sha256_hex(payload)
    key = object_key or f"docs/{relative_path}"
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
                rclone_remote
            ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
            """,
            (str(source),),
        )
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path=relative_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=1_700_000_000_000_000_000,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        file_row = catalog.get_file_by_path(2, relative_path)
        catalog.mark_local_copy_missing(
            file_row["id"], observed_at="2026-07-21T11:00:00+00:00"
        )
        version_id = catalog.record_archive_version(
            vault_id=2,
            path=relative_path,
            object_key=key,
            provider_version_id=provider_version_id,
            size=len(payload),
            storage_class=storage_class,
            etag="etag",
            uploaded_at="2026-07-21T10:00:00+00:00",
            observed_at="2026-07-21T10:00:00+00:00",
            scan_id="2026-07-21T10:00:00+00:00",
            origin="upload",
        )
        catalog.mark_version_verified(
            version_id,
            plaintext_sha256=digest,
            verified_at="2026-07-21T10:01:00+00:00",
        )
    return source, database_path, version_id


def _run_plain_recover(
    database_path: Path,
    *,
    relative_path: str,
    downloaded_bytes: bytes | None,
    provider_version_id: str = "s3-version-1",
    object_key: str = "docs/report.txt",
    storage_class: str = "STANDARD",
    restore_header: str | None = None,
) -> Mock:
    get_calls: list[dict] = []
    head_calls: list[dict] = []

    def head_object(**kwargs):
        head_calls.append(kwargs)
        response = {
            "ContentLength": len(downloaded_bytes or b""),
            "StorageClass": storage_class,
            "VersionId": provider_version_id,
        }
        if restore_header is not None:
            response["Restore"] = restore_header
        return response

    def get_object(**kwargs):
        get_calls.append(kwargs)
        if downloaded_bytes is None:
            raise RuntimeError("GetObject denied in test")
        return {
            "Body": _FakeBody(downloaded_bytes),
            "ContentLength": len(downloaded_bytes),
            "VersionId": kwargs.get("VersionId"),
        }

    client = Mock()
    client.head_object = Mock(side_effect=head_object)
    client.get_object = Mock(side_effect=get_object)
    client.restore_object = Mock()
    _attach_download_file(client)
    client.get_calls = get_calls
    client.head_calls = head_calls

    database_settings = SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
    )
    worker_settings = SimpleNamespace(
        operation_concurrency=1,
        restore_poll_interval=900,
        restore_days=3,
        restore_tier="Bulk",
        restore_high_impact_gib=100,
        restore_high_impact_eur=10.0,
        restore_approval_hold_seconds=3600,
        allow_local_delete=False,
    )
    with patch("app.database.settings", database_settings):
        queue_jobs(relative_path, "recover", 2, 1)
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.run_rclone") as run_rclone,
            patch("app.storage.s3_client", return_value=client),
        ):
            process_jobs_once()
            client.run_rclone = run_rclone
    return client


class PlainRecoveryVerificationTests(unittest.TestCase):
    def _interrupt_recovery(
        self,
        database_path: Path,
        *,
        relative_path: str,
    ) -> int:
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(database_path),
        )
        with patch("app.database.settings", database_settings):
            queued = queue_jobs(relative_path, "recover", 2, 1)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='verifying', claim_token='dead-worker',
                        claimed_at='2026-07-21T12:00:00+00:00',
                        claim_expires_at='2000-01-01T00:00:00+00:00'
                    WHERE id=%s
                    """,
                    (queued["job_ids"][0],),
                )
        return queued["job_ids"][0]

    def test_restart_adopts_matching_destination_without_deleting_local_copy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            job_id = self._interrupt_recovery(
                database_path, relative_path="report.txt"
            )
            destination = source / "report.txt"
            destination.write_bytes(payload)

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 1, "requeued": 0, "failed": 0})
            self.assertEqual(destination.read_bytes(), payload)
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                local = connection.execute(
                    """
                    SELECT presence, plaintext_sha256,
                           matched_archive_version_id
                    FROM local_copies WHERE vault_file_id=%s
                    """,
                    (observed["id"],),
                ).fetchone()
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()
            self.assertEqual(local["presence"], "present")
            self.assertEqual(local["plaintext_sha256"], _sha256_hex(payload))
            self.assertEqual(local["matched_archive_version_id"], version_id)
            self.assertEqual(job["status"], "completed")
            self.assertIn("adopted", (job["message"] or "").lower())

    def test_restart_preserves_mismatching_destination_as_explicit_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            job_id = self._interrupt_recovery(
                database_path, relative_path="report.txt"
            )
            destination = source / "report.txt"
            conflict = b"user-created-conflict"
            destination.write_bytes(conflict)

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 0, "requeued": 0, "failed": 1})
            self.assertEqual(destination.read_bytes(), conflict)
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()
            self.assertEqual(observed["local_copy"]["presence"], "missing")
            self.assertEqual(job["status"], "failed")
            self.assertIn("conflict", (job["message"] or "").lower())

    def test_restart_requeues_absent_destination_and_removes_only_owned_temps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            job_id = self._interrupt_recovery(
                database_path, relative_path="report.txt"
            )
            target = source / "report.txt"
            owned = target.with_name(f".report.txt.restore-{'a' * 32}.tmp")
            foreign = target.with_name(".report.txt.restore-not-owned.tmp")
            owned.write_bytes(b"partial")
            foreign.write_bytes(b"keep")

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 0, "requeued": 1, "failed": 0})
            self.assertFalse(target.exists())
            self.assertFalse(owned.exists())
            self.assertEqual(foreign.read_bytes(), b"keep")
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()
            self.assertEqual(job["status"], "queued")

    def test_restart_preserves_a_symlink_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            job_id = self._interrupt_recovery(
                database_path, relative_path="report.txt"
            )
            destination = source / "report.txt"
            destination.symlink_to(source / "outside.txt")

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary["failed"], 1)
            self.assertTrue(destination.is_symlink())
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()
            self.assertEqual(job["status"], "failed")

    def test_recover_verifies_digest_before_replacing_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            client = _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=payload,
            )

            target = source / "report.txt"
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.glob(".*.restore-*.tmp")), [])
            self.assertTrue(client.get_calls)
            self.assertEqual(client.get_calls[0]["VersionId"], "s3-version-1")
            self.assertEqual(client.get_calls[0]["Key"], "docs/report.txt")
            client.download_file.assert_called()
            download_kwargs = client.download_file.call_args.kwargs
            self.assertEqual(
                download_kwargs["ExtraArgs"]["VersionId"],
                "s3-version-1",
            )
            client.run_rclone.assert_not_called()

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                listed = catalog.list_file_rows(2)
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(
                observed["local_copy"]["plaintext_sha256"],
                _sha256_hex(payload),
            )
            file_row = next(row for row in listed if row["path"] == "report.txt")
            self.assertTrue(file_row["cleanup_eligible"])
            self.assertEqual(job["status"], "completed")

    def test_mismatched_recovery_digest_does_not_replace_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=b"tampered-bytes",
            )

            target = source / "report.txt"
            self.assertFalse(target.exists())
            self.assertEqual(list(source.glob("**/.*.restore-*.tmp")), [])

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "missing")
            self.assertEqual(job["status"], "failed")
            self.assertIn("digest", (job["message"] or "").lower())

    def test_path_only_rclone_download_is_never_used_for_plain_recovery(self) -> None:
        """Regression: current-key rclone copyto must not recover plain versions."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            _source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            client = _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=payload,
            )
            client.run_rclone.assert_not_called()
            self.assertTrue(
                client.download_file.called,
                msg="Recovery must use TransferManager download_file",
            )
            self.assertTrue(client.get_calls, msg="Recovery must call GetObject")
            self.assertTrue(
                all("VersionId" in call for call in client.get_calls),
                msg="Recovery must pin VersionId on GetObject",
            )
            self.assertTrue(
                all(
                    call.kwargs.get("ExtraArgs", {}).get("VersionId")
                    for call in client.download_file.call_args_list
                ),
                msg="Recovery must pin VersionId on download_file ExtraArgs",
            )

    def test_bug_012_recover_refuses_existing_destination(self) -> None:
        """[BUG-012][Req: REQ-024] do not replace over an existing on-disk file.

        Catalog local_presence may lag behind the filesystem (external recreate
        or incomplete free-space). Recover must fail closed when the destination
        already exists as a file/symlink, rather than Path.replace overwriting it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archived = b"archived-plaintext"
            preexisting = b"on-disk-local-copy"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=archived,
                object_key="docs/report.txt",
            )
            destination = source / "report.txt"
            destination.write_bytes(preexisting)

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
            self.assertEqual(observed["local_copy"]["presence"], "missing")

            _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=archived,
            )

            self.assertEqual(
                destination.read_bytes(),
                preexisting,
                "recover must not overwrite an existing on-disk Local Copy",
            )
            self.assertEqual(list(source.glob("**/.*.restore-*.tmp")), [])

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "missing")
            self.assertEqual(job["status"], "failed")
            self.assertRegex(
                (job["message"] or "").lower(),
                r"local copy.*already exists|already exists.*recovery destination",
            )

    def test_bug_013_crypt_download_uses_object_key(self) -> None:
        """[BUG-013][Req: REQ-025] crypt rclone path derives from object_key.

        After a rename, ``job['path']`` is the current Local Copy path while the
        Archive Version ``object_key`` still names the pre-rename crypt object.
        Exact-version crypt download must build the rclone source from
        ``object_key`` (via name decode), not solely from ``job['path']``.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "recovered.tmp"
            job = {
                "id": 913,
                "path": "renamed.txt",
                "s3_prefix": "docs",
                "s3_bucket": "bucket",
                "rclone_remote": "base",
                "encryption_mode": "crypt",
            }
            object_key = "docs/nq/old-encrypted"
            logical_from_key = "original.txt"
            fake_config = RuntimeRcloneConfig(
                path=root / "fake.rclone.conf",
                remote_name="vault",
                config_text="[vault]\ntype = crypt\n",
                secrets=None,
            )
            fake_config.path.write_text(fake_config.config_text, encoding="utf-8")
            rclone_sources: list[str] = []

            @contextmanager
            def fake_vault_rclone_config(_vault):
                yield fake_config

            def fake_rclone(command, source, destination, *args, **kwargs) -> None:
                self.assertEqual(command, "copyto")
                rclone_sources.append(str(source))
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"recovered-bytes")

            with (
                patch(
                    "app.storage.vault_rclone_config",
                    side_effect=fake_vault_rclone_config,
                ),
                patch(
                    "app.storage.decode_object_relative_path",
                    return_value=logical_from_key,
                ),
                patch("app.storage.run_rclone", side_effect=fake_rclone),
            ):
                download_exact_version_plaintext(
                    job,
                    object_key=object_key,
                    provider_version_id="crypt-version-1",
                    temporary=temporary,
                )

            self.assertEqual(
                rclone_sources,
                [f"vault:{logical_from_key}"],
                "crypt download must address the Archive Version object_key path",
            )
            self.assertNotIn(
                "renamed.txt",
                rclone_sources[0] if rclone_sources else "",
                "crypt download must not use only the post-rename job path",
            )
            self.assertTrue(temporary.is_file())

    def test_bug_013_content_crypt_download_uses_object_key(self) -> None:
        """[BUG-013][Req: REQ-025] content-crypt rclone path from object_key.

        Legacy content-crypt remotes (no per-vault name encryption) still must
        address the Archive Version key (``.bin`` stripped), not solely the
        current ``job['path']`` after a rename.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "recovered.tmp"
            job = {
                "id": 914,
                "path": "renamed.txt",
                "s3_prefix": "docs",
                "s3_bucket": "bucket",
                "rclone_remote": "crypt-remote",
                # Legacy vault: content crypt via rclone remote, no name encryption.
                "encryption_mode": None,
            }
            object_key = "docs/original.txt.bin"
            rclone_sources: list[str] = []

            def fake_rclone(command, source, destination, *args, **kwargs) -> None:
                self.assertEqual(command, "copyto")
                rclone_sources.append(str(source))
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"recovered-bytes")

            with (
                patch("app.storage.rclone_remote_is_crypt", return_value=True),
                patch("app.storage.run_rclone", side_effect=fake_rclone),
            ):
                download_exact_version_plaintext(
                    job,
                    object_key=object_key,
                    provider_version_id="crypt-version-2",
                    temporary=temporary,
                )

            self.assertEqual(
                rclone_sources,
                ["crypt-remote:original.txt"],
                "content-crypt download must strip object_key to the logical path",
            )
            self.assertNotIn(
                "renamed.txt",
                rclone_sources[0] if rclone_sources else "",
            )
            self.assertTrue(temporary.is_file())


class GlacierRestoreWorkflowTests(unittest.TestCase):
    def test_glacier_version_requests_restore_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"cold-archive"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="cold.txt",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )
            client = _run_plain_recover(
                database_path,
                relative_path="cold.txt",
                downloaded_bytes=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )

            client.restore_object.assert_called_once()
            restore_kwargs = client.restore_object.call_args.kwargs
            self.assertEqual(restore_kwargs["VersionId"], "s3-version-1")
            self.assertEqual(restore_kwargs["Key"], "docs/cold.txt")
            self.assertEqual(
                restore_kwargs["RestoreRequest"]["GlacierJobParameters"]["Tier"],
                "Bulk",
            )
            client.get_object.assert_not_called()
            self.assertFalse((source / "cold.txt").exists())

            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("cold.txt",),
                ).fetchone()
                version = connection.execute(
                    "SELECT restore_state FROM archive_versions WHERE id=%s",
                    (version_id,),
                ).fetchone()

            self.assertEqual(job["status"], "restoring")
            self.assertIn("cannot be cancelled", (job["message"] or "").lower())
            self.assertEqual(version["restore_state"], "restoring")

    def test_bug_018_cancel_preserves_restore_state(self) -> None:
        """[BUG-018][Req: REQ-030] cancel must not wipe Glacier restore_state.

        Seam: ``cancel_job_group`` for action ``recover``, observed through Job
        status and Archive Version ``restore_state`` / ``restore_expiry`` on a
        real SQLite catalog (same cancel path as ``/api/jobs/cancel``).

        After RestoreObject has been accepted, cancel must stop the Job but
        retain restore polling/expiry context for a later recover.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"cold-archive"
            _source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="cold.txt",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )
            restore_expiry = "2026-07-28T12:00:00+00:00"
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", database_settings):
                queued = queue_jobs("cold.txt", "recover", 2, 1)
                with SQLiteConnection(str(database_path)) as connection:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status='restoring',
                            message=%s,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (
                            "Waiting for Glacier restore; RestoreObject cannot "
                            "be cancelled after AWS accepts it",
                            "2026-07-24T10:00:00+00:00",
                            queued["job_ids"][0],
                        ),
                    )
                    ArchiveCatalog(connection).update_restore_state(
                        version_id,
                        state="restoring",
                        expiry=restore_expiry,
                        checked_at="2026-07-24T10:00:00+00:00",
                        storage_class="GLACIER",
                    )

                result = cancel_job_group(
                    queued["group_id"],
                    "recover",
                    {"id": 2, "role": "owner", "member_user_id": 1},
                )

            self.assertEqual(result["cancelled_count"], 1)
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, group_id FROM jobs WHERE id=%s",
                    (queued["job_ids"][0],),
                ).fetchone()
                version = connection.execute(
                    """
                    SELECT restore_state, restore_expiry
                    FROM archive_versions WHERE id=%s
                    """,
                    (version_id,),
                ).fetchone()

            self.assertEqual(job["status"], "cancelled")
            self.assertEqual(
                version["restore_state"],
                "restoring",
                "cancel must retain non-cancellable RestoreObject restore_state",
            )
            self.assertEqual(
                version["restore_expiry"],
                restore_expiry,
                "cancel must retain restore_expiry for subsequent polling",
            )

    def test_restored_glacier_version_downloads_after_restore_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"cold-archive"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="cold.txt",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("cold.txt", "recover", 2, 1)
                with SQLiteConnection(str(database_path)) as connection:
                    connection.execute(
                        "UPDATE jobs SET status='restoring', updated_at=%s",
                        ("2020-01-01T00:00:00+00:00",),
                    )

            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": len(payload),
                    "StorageClass": "GLACIER",
                    "VersionId": "s3-version-1",
                    "Restore": 'ongoing-request="false", expiry-date="Fri, 01 Aug 2026 00:00:00 GMT"',
                }
            )
            client.get_object = Mock(
                return_value={"Body": _FakeBody(payload), "ContentLength": len(payload)}
            )
            client.restore_object = Mock()
            _attach_download_file(client)
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=1,
                restore_days=3,
                restore_tier="Bulk",
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch("app.storage.run_rclone"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            client.restore_object.assert_not_called()
            client.download_file.assert_called_once()
            client.get_object.assert_called_once()
            self.assertEqual((source / "cold.txt").read_bytes(), payload)
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s", ("cold.txt",)
                ).fetchone()
                version = connection.execute(
                    "SELECT restore_state FROM archive_versions WHERE id=%s",
                    (version_id,),
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(version["restore_state"], "available")


if __name__ == "__main__":
    unittest.main()
