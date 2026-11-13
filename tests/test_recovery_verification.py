"""Digest-verified recovery of an exact Archive Version (issue #4).

Seams under test:
- Recovery Job worker (`process_recover` / `process_jobs_once`) observed through
  Job status and Local Copy fingerprint via ArchiveCatalog.
- Exact-version download adapter (`download_exact_version_plaintext`): must pin
  S3 VersionId; path-only current-object download is forbidden. Crypt recover
  must address S3 from Archive Version ``object_key``, not solely
  ``job['path']`` (BUG-013 / REQ-025).
- System boundaries mocked: S3 GetObject/HeadObject and the SDK.

BUG-012 (REQ-024) uses the same recovery worker seam: catalog absence plus a
real on-disk destination must fail closed without overwrite.

BUG-018 (REQ-030) uses the public cancel seam ``cancel_job_group``: after a
recover Job has persisted Glacier ``restore_state`` / expiry (RestoreObject
accepted and non-cancellable), cancel must leave that polling context intact
while still cancelling the Job.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import hashlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog

from app.main import cancel_job_group, queue_jobs
from app.storage import (
    download_exact_version_plaintext,
    process_jobs_once,
    reconcile_interrupted_jobs,
)
from app.services.vault_relocation import enroll_vault_root_identity
from tests.catalog_support import seed_vault
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
    """A Vault 2 (``Docs``) whose file's verified Current is Cloud-only (#594)."""
    source = root / "source"
    source.mkdir()
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    with PostgresConnection(str(database_path)) as connection:
        seed_vault(connection, vault_id=2, source_root=str(source))
        enroll_vault_root_identity(connection, 2, str(source))
    version_id = _add_cloud_only_version(
        database_path, relative_path=relative_path, payload=payload,
        storage_class=storage_class, provider_version_id=provider_version_id,
        object_key=object_key or f"docs/{relative_path}",
    )
    return source, database_path, version_id


def _add_cloud_only_version(
    database_path: Path, *, relative_path: str, payload: bytes, storage_class: str,
    provider_version_id: str, object_key: str,
) -> str:
    """One more file of Vault 2 whose verified Current is its only copy."""
    with PostgresConnection(str(database_path)) as connection:
        catalog = ArchiveCatalog(connection)
        file_id = catalog.observe_local_copy(
            vault_id=2,
            path=relative_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=1_700_000_000_000_000_000,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        generation = catalog.get_file_by_path(2, relative_path)["generation"]
        version_id = catalog.begin_replacement(
            file_id, object_key=object_key, provider_version_id=provider_version_id,
            size=len(payload), storage_class=storage_class,
            created_at="2026-07-21T10:00:00+00:00",
        )
        catalog.promote_replacement(
            file_id, archive_version_id=version_id, plaintext_sha256=_sha256_hex(payload),
            verified_at="2026-07-21T10:01:00+00:00", local_generation=generation,
        )
        catalog.mark_local_copy_missing(file_id, observed_at="2026-07-21T11:00:00+00:00")
    return version_id


def _run_plain_recover(
    database_path: Path,
    *,
    relative_path: str,
    downloaded_bytes: bytes | None,
    provider_version_id: str = "s3-version-1",
    object_key: str = "docs/report.txt",
    storage_class: str = "STANDARD",
    restore_header: str | None = None,
    downloaded_mtime: float | None = None,
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
    if downloaded_mtime is not None:
        write_download = client.download_file.side_effect

        def download_with_original_mtime(*args, **kwargs) -> None:
            # Mirrors an SDK restoring the object's stored modification time.
            write_download(*args, **kwargs)
            filename = kwargs.get("Filename") or args[2]
            os.utime(filename, (downloaded_mtime, downloaded_mtime))

        client.download_file.side_effect = download_with_original_mtime
    client.get_calls = get_calls
    client.head_calls = head_calls

    database_settings = SimpleNamespace(
        database_dsn=database_dsn(str(database_path)),
    )
    worker_settings = SimpleNamespace(
        operation_concurrency=1,
        restore_poll_interval=900,
        restore_days=3,
        restore_tier="Bulk",
        restore_high_impact_gib=100,
        restore_high_impact_eur=10.0,
        restore_approval_hold_seconds=3600,
    )
    with patch("app.database.settings", database_settings):
        queue_jobs(relative_path, "recover", 2, 1)
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.upload_s3_file") as sdk_upload,
            patch("app.storage.s3_client", return_value=client),
        ):
            process_jobs_once()
            client.sdk_upload = sdk_upload
    return client


class PlainRecoveryVerificationTests(unittest.TestCase):
    def _interrupt_recovery(
        self,
        database_path: Path,
        *,
        relative_path: str,
    ) -> int:
        database_settings = SimpleNamespace(
            database_dsn=database_dsn(str(database_path)),
        )
        with patch("app.database.settings", database_settings):
            queued = queue_jobs(relative_path, "recover", 2, 1)
            with PostgresConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    UPDATE jobs SET status='verifying' WHERE id=%s
                    """,
                    (queued["job_ids"][0],),
                )
                # A dead worker's expired lease.
                connection.execute(
                    """
                    INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                    VALUES (%s, gen_random_uuid(), '1999-12-31T23:00:00+00:00',
                            '2000-01-01T00:00:00+00:00')
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
                database_dsn=database_dsn(str(database_path))
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 1, "requeued": 0, "failed": 0})
            self.assertEqual(destination.read_bytes(), payload)
            with PostgresConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                local = connection.execute(
                    """
                    SELECT local_presence AS presence, local_sha256 AS plaintext_sha256,
                           matched_version_id AS matched_archive_version_id
                    FROM files WHERE id=%s
                    """,
                    (observed["id"],),
                ).fetchone()
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()
            self.assertEqual(local["presence"], "present")
            self.assertEqual(bytes(local["plaintext_sha256"]).hex(), _sha256_hex(payload))
            self.assertEqual(str(local["matched_archive_version_id"]), str(version_id))
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
                database_dsn=database_dsn(str(database_path))
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 0, "requeued": 0, "failed": 1})
            self.assertEqual(destination.read_bytes(), conflict)
            with PostgresConnection(str(database_path)) as connection:
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
                database_dsn=database_dsn(str(database_path))
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 0, "requeued": 1, "failed": 0})
            self.assertFalse(target.exists())
            self.assertFalse(owned.exists())
            self.assertEqual(foreign.read_bytes(), b"keep")
            with PostgresConnection(str(database_path)) as connection:
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
                database_dsn=database_dsn(str(database_path))
            )
            with patch("app.database.settings", database_settings):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary["failed"], 1)
            self.assertTrue(destination.is_symlink())
            with PostgresConnection(str(database_path)) as connection:
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
            client.sdk_upload.assert_not_called()

            with PostgresConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                listed = catalog.list_file_rows(2)
                job = connection.execute(
                    "SELECT j.status, j.message FROM jobs j JOIN files f ON f.id=j.file_id WHERE f.path=%s",
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

    def test_recovered_file_with_original_mtime_is_not_freed_again(self) -> None:
        """#409: a recovery starts a full retention window, whatever the mtime.

        The Archive Version was verified long ago and the download restores
        the file's original 2019 modification time, as S3 metadata may. Automatic
        cleanup must still wait a full retention period from the recovery.
        """
        from app.services.operation_policies import (
            OperationPolicy,
            queue_auto_local_cleanups,
            set_policy,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"photo-from-2019"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="photo.jpg",
                payload=payload,
                object_key="docs/photo.jpg",
            )
            original_mtime = datetime(
                2019, 5, 1, 12, 0, tzinfo=timezone.utc
            ).timestamp()
            before_recovery = datetime.now(timezone.utc)
            _run_plain_recover(
                database_path,
                relative_path="photo.jpg",
                downloaded_bytes=payload,
                downloaded_mtime=original_mtime,
            )
            target = source / "photo.jpg"
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.stat().st_mtime, original_mtime)

            with PostgresConnection(str(database_path)) as connection:
                connection.execute("DELETE FROM notifications WHERE job_id IS NOT NULL")
                connection.execute("DELETE FROM jobs")
                set_policy(
                    connection,
                    2,
                    OperationPolicy(
                        local_release_after_days=90,
                    ),
                )
                next_pass = queue_auto_local_cleanups(
                    connection,
                    vault_id=2,
                    requested_by=1,
                    now=before_recovery + timedelta(days=1),
                )
                after_retention = queue_auto_local_cleanups(
                    connection,
                    vault_id=2,
                    requested_by=1,
                    now=before_recovery + timedelta(days=91),
                )

            self.assertEqual(next_pass, 0)
            self.assertEqual(after_retention, 1)
            self.assertEqual(
                target.stat().st_mtime,
                original_mtime,
                "recovery must not rewrite the user's modification time",
            )

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

            with PostgresConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT j.status, j.message FROM jobs j JOIN files f ON f.id=j.file_id WHERE f.path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "missing")
            self.assertEqual(job["status"], "failed")
            self.assertIn("digest", (job["message"] or "").lower())

    def test_path_only_current_key_download_is_never_used_for_plain_recovery(self) -> None:
        """Regression: current-key upload must not recover plain versions."""
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
            client.sdk_upload.assert_not_called()
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

            with PostgresConnection(str(database_path)) as connection:
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

            with PostgresConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT j.status, j.message FROM jobs j JOIN files f ON f.id=j.file_id WHERE f.path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "missing")
            self.assertEqual(job["status"], "failed")
            self.assertRegex(
                (job["message"] or "").lower(),
                r"local copy.*already exists|already exists.*recovery destination",
            )

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

            with PostgresConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT j.status, j.message FROM jobs j JOIN files f ON f.id=j.file_id WHERE f.path=%s",
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
        real PostgreSQL catalog (same cancel path as ``/api/jobs/cancel``).

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
                database_dsn=database_dsn(str(database_path)),
            )
            with patch("app.database.settings", database_settings):
                queued = queue_jobs("cold.txt", "recover", 2, 1)
                with PostgresConnection(str(database_path)) as connection:
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
                    {"id": 2},
                    1,
                )

            self.assertEqual(result["cancelled_count"], 1)
            with PostgresConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, group_id FROM jobs WHERE id=%s",
                    (queued["job_ids"][0],),
                ).fetchone()
                version = connection.execute(
                    """
                    SELECT restore_state, restore_expiry, restore_checked_at
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
            # The expiry exists only once the temporary copy is available
            # (schema: restore_expiry implies restore_state='available'); the
            # polling context a later recover needs is the observed check.
            self.assertIsNone(version["restore_expiry"])
            self.assertEqual(
                version["restore_checked_at"],
                datetime.fromisoformat("2026-07-24T10:00:00+00:00"),
                "cancel must retain the restore polling context",
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
                database_dsn=database_dsn(str(database_path)),
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("cold.txt", "recover", 2, 1)
                with PostgresConnection(str(database_path)) as connection:
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
                patch("app.storage.upload_s3_file"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            client.restore_object.assert_not_called()
            client.download_file.assert_called_once()
            client.get_object.assert_called_once()
            self.assertEqual((source / "cold.txt").read_bytes(), payload)
            with PostgresConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT j.status FROM jobs j JOIN files f ON f.id=j.file_id WHERE f.path=%s", ("cold.txt",)
                ).fetchone()
                version = connection.execute(
                    "SELECT restore_state FROM archive_versions WHERE id=%s",
                    (version_id,),
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(version["restore_state"], "available")


if __name__ == "__main__":
    unittest.main()
