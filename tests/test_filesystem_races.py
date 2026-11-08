"""Catalog integration for #472/#476/#477; deterministic interleavings, no S3 network."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import hashlib
import io
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import boto3
from boto3.s3.transfer import TransferConfig
from s3transfer.utils import OSUtils
from watchfiles import Change, awatch

from app import storage
from app.catalog import ArchiveCatalog
from tests import test_auto_upload_watcher as fixtures


class FilesystemCatalogTests(unittest.TestCase):
    setUp = fixtures.WatcherAutomaticUploadTests.setUp
    db = fixtures.WatcherAutomaticUploadTests.db
    write = fixtures.WatcherAutomaticUploadTests.write
    advance = fixtures.WatcherAutomaticUploadTests.advance
    queue = fixtures.WatcherAutomaticUploadTests.queue
    jobs = fixtures.WatcherAutomaticUploadTests.jobs

    def entries(self):
        with self.db() as connection:
            return connection.execute("""
                SELECT fp.path, lc.* FROM vault_files vf
                JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                JOIN local_copies lc ON lc.vault_file_id=vf.id
                WHERE vf.vault_id=2 AND vf.status='active' ORDER BY fp.path
            """).fetchall()

    def archive(self, name="photo.jpg", content=b"photo", *, missing=False):
        target = self.write(name, content)
        digest = hashlib.sha256(content).hexdigest()
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            version = catalog.record_archive_version(
                vault_id=2, path=name, object_key=f"photos/{name}", provider_version_id="exact",
                size=len(content), storage_class="STANDARD", etag="etag",
                uploaded_at=self.clock.isoformat(), observed_at=self.clock.isoformat(), scan_id="first",
            )
            catalog.mark_version_verified(version, plaintext_sha256=digest, verified_at=self.clock.isoformat())
            catalog.set_local_fingerprint(vault_id=2, path=name, plaintext_sha256=digest, matched_archive_version_id=version)
        if missing:
            target.unlink()
            storage.apply_filesystem_changes(self.vault, {(Change.deleted, str(target))})
        return target, version, digest

    def job(self, action, path="photo.jpg"):
        with self.db() as connection:
            ids, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path=path, action=action, requested_by=1,
                requested_at=self.clock.isoformat(), group_id="filesystem-regression", is_directory=False,
            )
            return dict(connection.execute("SELECT j.*, v.source_root, v.s3_bucket, v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (ids[0],)).fetchone())

    def test_directory_move_in_rename_and_move_out_are_catalogued_without_scan(self):
        incoming = self.base / "incoming"
        (incoming / "deep").mkdir(parents=True)
        (incoming / "deep" / "photo.jpg").write_bytes(b"photo")
        os.utime(incoming / "deep" / "photo.jpg", (1_500_000_000, 1_500_000_000))
        incoming.rename(self.root / "incoming")
        storage.apply_filesystem_changes(self.vault, {(Change.added, str(self.root / "incoming"))})
        self.assertEqual([(row["path"], row["presence"]) for row in self.entries()], [("incoming/deep/photo.jpg", "present")])
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        (self.root / "incoming").rename(self.root / "renamed")
        storage.apply_filesystem_changes(self.vault, {(Change.deleted, str(self.root / "incoming")), (Change.added, str(self.root / "renamed"))})
        self.assertEqual([(row["path"], row["presence"]) for row in self.entries()], [("incoming/deep/photo.jpg", "missing"), ("renamed/deep/photo.jpg", "present")])
        (self.root / "renamed").rename(self.base / "outside")
        storage.apply_filesystem_changes(self.vault, {(Change.deleted, str(self.root / "renamed"))})
        self.assertTrue(all(row["presence"] == "missing" for row in self.entries()))

    def test_subtree_batches_preserve_siblings_and_do_not_follow_symlinks(self):
        self.write("sibling.jpg")
        self.write("new/deep/a.jpg", watch=False)
        self.write("new/deep/b.jpg", watch=False)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(b"outside")
        (self.root / "new" / "link").symlink_to(outside, target_is_directory=True)
        with patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 1):
            storage.apply_filesystem_changes(self.vault, {(Change.added, str(self.root / "new")), (Change.added, str(self.root / "new" / "deep"))})
        rows = {row["path"]: row for row in self.entries()}
        self.assertEqual(set(rows), {"sibling.jpg", "new/deep/a.jpg", "new/deep/b.jpg", "new/link"})
        self.assertEqual(rows["sibling.jpg"]["presence"], "present")
        self.assertEqual(rows["new/link"]["presence"], "unsupported")

    def test_directory_metadata_event_does_not_unprotect_verified_children(self):
        target, version, digest = self.archive("existing/photo.jpg")
        self.assertEqual(storage.apply_filesystem_changes(self.vault, {(Change.modified, str(target.parent))}), 0)
        row = self.entries()[0]
        self.assertEqual((row["plaintext_sha256"], row["matched_archive_version_id"]), (digest, version))

    def test_subtree_root_replacement_compensates_already_committed_batches(self):
        self.write("sibling.jpg")
        self.write("new/a.jpg", watch=False)
        self.write("new/b.jpg", watch=False)
        original_inspect = storage.inspect_entry
        inspected_files = 0

        def replace_root_after_first_batch(entry):
            nonlocal inspected_files
            result = original_inspect(entry)
            if result and result[0] == "regular":
                inspected_files += 1
                if inspected_files == 2:
                    self.root.rename(self.base / "old-root")
                    self.root.mkdir()
            return result

        with (patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 1),
              patch("app.storage.inspect_entry", side_effect=replace_root_after_first_batch)):
            with self.assertRaises(RuntimeError):
                storage.apply_filesystem_changes(self.vault, {(Change.added, str(self.root / "new"))})
        self.assertEqual([row["path"] for row in self.entries()], ["sibling.jpg"])

    def test_subtree_component_swap_compensates_committed_catalog_batches(self):
        self.write("sibling.jpg")
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.write(f"incoming/{name}", watch=False)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside-only.txt").write_bytes(b"outside")
        real_inspect = storage.inspect_entry
        inspected = 0

        def swap_after_batch(entry):
            nonlocal inspected
            result = real_inspect(entry)
            if result and result[0] == "regular":
                inspected += 1
                if inspected == 2:
                    (self.root / "incoming").rename(self.base / "detached")
                    (self.root / "incoming").symlink_to(outside, target_is_directory=True)
            return result

        with (patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 1),
              patch("app.storage.inspect_entry", side_effect=swap_after_batch)):
            with self.assertRaises(RuntimeError):
                storage.apply_filesystem_changes(self.vault, {(Change.added, str(self.root / "incoming"))})
        self.assertEqual([row["path"] for row in self.entries()], ["sibling.jpg"])

    def test_replaced_directory_invalidates_identical_size_and_mtime_content(self):
        target, _, _ = self.archive("new/photo.jpg")
        before = target.stat()
        (self.root / "new").rename(self.base / "previous")
        target.parent.mkdir()
        target.write_bytes(b"other")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        storage.apply_filesystem_changes(self.vault, {(Change.added, str(target.parent))})
        row = self.entries()[0]
        self.assertIsNone(row["plaintext_sha256"])
        self.assertIsNone(row["matched_archive_version_id"])
        self.assertEqual(row["presence"], "present")

    def test_free_space_with_watcher_during_hash_keeps_verified_state(self):
        target, version, digest = self.archive()
        job = self.job("free-space")
        real_hash = storage.hash_stable_regular_file
        observed = []
        threads = []
        errors = []

        def watch_during_hash(path):
            observed.append(path)
            started = threading.Event()
            def watch():
                started.set()
                try:
                    storage.apply_filesystem_changes(self.vault, {(Change.deleted, str(target))})
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=watch)
            threads.append(thread)
            thread.start()
            self.assertTrue(started.wait(2))
            if not storage.scan_lock_for_vault(2).locked():
                thread.join(2)
                self.assertFalse(thread.is_alive())
            return real_hash(path)

        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.local_release_is_confirmed", return_value=True),
              patch("app.storage.s3_client") as client,
              patch("app.storage.hash_stable_regular_file", side_effect=watch_during_hash)):
            client.return_value.head_object.return_value = {"ContentLength": 5, "VersionId": "exact"}
            try:
                storage.process_free_space(job)
            finally:
                for thread in threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(observed)
        self.assertFalse(target.exists())
        row = self.entries()[0]
        self.assertEqual(row["presence"], "missing")
        self.assertEqual(row["plaintext_sha256"], digest)
        self.assertEqual(row["matched_archive_version_id"], version)

    def test_failed_release_is_immediately_protected_even_after_queued_watcher_events(self):
        target, version, digest = self.archive()
        job = self.job("free-space")
        real_check = storage._checked_local_release_target
        calls = 0

        def fail_final_check(*args, **kwargs):
            nonlocal calls
            # The helper's connection=None entry recurses into itself. Count
            # actual checks, not wrapper entries, to inject AFTER the rename.
            if kwargs.get("connection") is not None:
                calls += 1
                if calls == 2:
                    self.assertFalse(target.exists())
                    raise RuntimeError("Injected final protection failure")
            return real_check(*args, **kwargs)

        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.local_release_is_confirmed", return_value=True),
              patch("app.storage.s3_client") as client,
              patch("app.storage._checked_local_release_target", side_effect=fail_final_check)):
            client.return_value.head_object.return_value = {"ContentLength": 5, "VersionId": "exact"}
            with self.assertRaisesRegex(RuntimeError, "Injected final"):
                storage.process_free_space(job)
        self.assertEqual(calls, 2)
        self.assertEqual(target.read_bytes(), b"photo")
        # Real observers can queue the deletion before rollback and emit the
        # addition in a later batch. Do not combine them into one path set.
        for change in (Change.deleted, Change.added, Change.modified):
            storage.apply_filesystem_changes(self.vault, {(change, str(target))})
            row = self.entries()[0]
            self.assertEqual((row["presence"], row["plaintext_sha256"], row["matched_archive_version_id"], row["cloud_only_version_id"]), ("present", digest, version, None))

    def test_failed_release_reconciles_external_replacement_and_keeps_salvage(self):
        target, version, _ = self.archive()
        job = self.job("free-space")
        real_hash = storage.hash_stable_regular_file
        def replace_then_fail(path):
            if path != target:
                target.write_bytes(b"other")
                raise RuntimeError("Injected hash failure")
            return real_hash(path)
        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.local_release_is_confirmed", return_value=True),
              patch("app.storage.s3_client") as client,
              patch("app.storage.hash_stable_regular_file", side_effect=replace_then_fail)):
            client.return_value.head_object.return_value = {"ContentLength": 5, "VersionId": "exact"}
            with self.assertRaisesRegex(RuntimeError, "original content was preserved"):
                storage.process_free_space(job)
        storage.apply_filesystem_changes(self.vault, {(Change.added, str(target))})
        row = self.entries()[0]
        self.assertEqual(row["presence"], "present")
        self.assertIsNone(row["matched_archive_version_id"])
        self.assertEqual(row["plaintext_sha256"], hashlib.sha256(b"other").hexdigest())
        self.assertEqual(row["cloud_only_version_id"], version)
        claims = list(target.parent.glob(".*.cleanup-*.tmp"))
        self.assertEqual([path.read_bytes() for path in claims], [b"photo"])

    def test_real_multipart_transfer_with_watcher_has_no_temporary_catalog_entries(self):
        payload = b"multipart payload" * 1024
        target, version, digest = self.archive("new/photo.jpg", content=payload, missing=True)
        target.parent.rmdir()  # Recover must recreate the directory as well.
        job = self.job("recover", path="new/photo.jpg")
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        ranges = []
        sdk_temporaries = []
        original_rename = OSUtils.rename_file

        def get_object(**kwargs):
            ranges.append(kwargs.get("Range"))
            start, end = kwargs.get("Range", "bytes=0-").removeprefix("bytes=").split("-")
            content = payload[int(start): int(end) + 1 if end else None]
            return {"Body": io.BytesIO(content), "ContentLength": len(content)}

        def rename_with_watcher(osutils, source, destination):
            sdk_temporaries.append(source)
            storage.apply_filesystem_changes(self.vault, {(Change.added, source)})
            original_rename(osutils, source, destination)
            storage.apply_filesystem_changes(self.vault, {(Change.deleted, source)})

        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.s3_client", return_value=client),
              patch.object(client, "head_object", return_value={"ContentLength": len(payload), "VersionId": "exact"}),
              patch.object(client, "get_object", side_effect=get_object),
              patch("app.storage.s3_download_transfer_config", return_value=TransferConfig(multipart_threshold=1024, multipart_chunksize=4096, use_threads=False)),
              patch.object(OSUtils, "rename_file", rename_with_watcher)):
            storage.process_recover(job)
        self.assertGreater(len(ranges), 1)
        self.assertTrue(sdk_temporaries)
        self.assertEqual(target.read_bytes(), payload)
        storage.apply_filesystem_changes(self.vault, {
            (Change.added, str(target.parent)), (Change.added, str(target)),
        })
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.entries()[0]["plaintext_sha256"], digest)
        self.assertEqual(self.entries()[0]["matched_archive_version_id"], version)
        storage.apply_filesystem_changes(self.vault, {(Change.modified, str(target))})
        self.assertEqual(self.entries()[0]["plaintext_sha256"], digest)
        self.assertEqual(self.entries()[0]["matched_archive_version_id"], version)

    def test_existing_restore_ghosts_are_hidden_but_real_user_names_are_preserved(self):
        target, version, _ = self.archive()
        with self.db() as connection:
            connection.execute("""INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by,
                requested_at, updated_at, group_id, archive_version_id) SELECT 2, vf.id, 'photo.jpg', 'recover', 'completed', 1,
                %s, %s, 'old-recovery', %s FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id
                WHERE fp.path='photo.jpg'""", (self.clock.isoformat(), self.clock.isoformat(), version))
            catalog = ArchiveCatalog(connection)
            for name in (f".photo.jpg.restore-{'a' * 32}.tmp.f8Fc29bA", "photo.jpg.tmp.f8Fc29bA", "user.tmp.f8Fc29bA"):
                catalog.observe_local_copy(vault_id=2, path=name, file_type="regular", size=5, mtime_ns=1, observed_at=self.clock.isoformat())
        # Repair is part of ordinary local reconciliation, not a destructive
        # schema migration and never a broad *.tmp.* filename filter.
        storage.scan_tree(self.vault, self.clock.isoformat())
        self.assertEqual({row["path"] for row in self.entries()}, {"photo.jpg", "user.tmp.f8Fc29bA"})
        storage.scan_tree(self.vault, self.clock.isoformat())
        self.assertEqual({row["path"] for row in self.entries()}, {"photo.jpg", "user.tmp.f8Fc29bA"})

    def test_ghost_repair_preserves_on_disk_and_cloud_backed_reserved_names(self):
        on_disk = f".photo.jpg.restore-{'b' * 32}.tmp.f8Fc29bA"
        cloud_backed = f".photo.jpg.restore-{'c' * 32}.tmp.f8Fc29bA"
        self.write(on_disk)
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(vault_id=2, path=on_disk, file_type="regular", size=5, mtime_ns=1, observed_at=self.clock.isoformat())
            catalog.record_archive_version(vault_id=2, path=cloud_backed, object_key=f"photos/{cloud_backed}", provider_version_id="keep", size=5, storage_class="STANDARD", etag="etag", uploaded_at=self.clock.isoformat(), observed_at=self.clock.isoformat(), scan_id="legacy")
            catalog.observe_local_copy(vault_id=2, path=cloud_backed, file_type="regular", size=5, mtime_ns=1, observed_at=self.clock.isoformat())
        storage.scan_tree(self.vault, self.clock.isoformat())
        self.assertEqual({row["path"] for row in self.entries()}, {on_disk, cloud_backed})
        self.assertEqual((self.root / on_disk).read_bytes(), b"photo")


class LiveWatcherCatalogTests(unittest.TestCase):
    setUp = fixtures.WatcherAutomaticUploadTests.setUp
    db = fixtures.WatcherAutomaticUploadTests.db
    entries = FilesystemCatalogTests.entries
    advance = fixtures.WatcherAutomaticUploadTests.advance
    queue = fixtures.WatcherAutomaticUploadTests.queue

    def test_new_directory_then_later_copy_is_protected_on_both_backends(self):
        for force_polling in (False, True):
            with self.subTest(force_polling=force_polling):
                asyncio.run(self.check_new_directory(force_polling))
        self.advance(300)
        self.assertEqual(self.queue(), 4)

    def test_sibling_mutations_survive_topology_refresh_on_both_backends(self):
        for force_polling in (False, True):
            with self.subTest(force_polling=force_polling):
                asyncio.run(self.check_new_directory(force_polling, mutate_siblings=True))

    async def check_new_directory(self, force_polling, *, mutate_siblings=False):
        ready = asyncio.Event()
        siblings_seen = asyncio.Event()
        directory_seen = asyncio.Event()
        file_seen = asyncio.Event()
        later_seen = asyncio.Event()
        directory = self.root / str(force_polling)
        target = directory / "nested" / "later.jpg"
        later = directory / "nested" / "after-rearm.jpg"
        loop = asyncio.get_running_loop()
        real_apply = storage.apply_filesystem_changes
        sibling_dir = self.root / f"siblings-{force_polling}"
        if mutate_siblings:
            sibling_dir.mkdir()
            for name in ("existing.jpg", "deleted.jpg", "before-rename.jpg"):
                path = sibling_dir / name
                path.write_bytes(b"first")
                real_apply(self.vault, {(Change.added, str(path))})
            with self.db() as connection:
                ArchiveCatalog(connection).set_local_fingerprint(
                    vault_id=2, path=f"{sibling_dir.name}/existing.jpg",
                    plaintext_sha256=hashlib.sha256(b"first").hexdigest(), matched_archive_version_id=None,
                )

        injected = False
        def inject_changes():
            nonlocal injected
            injected = True
            if mutate_siblings:
                path = sibling_dir / "existing.jpg"
                original = path.stat()
                path.write_bytes(b"changed sibling")
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
                (sibling_dir / "deleted.jpg").unlink()
                (sibling_dir / "before-rename.jpg").rename(sibling_dir / "after-rename.jpg")
            target.write_bytes(b"photo")
            os.utime(target, (1_500_000_000, 1_500_000_000))

        watch_calls = 0
        async def ticking_watch(*args, **kwargs):
            nonlocal watch_calls
            watch_calls += 1
            kwargs.update(yield_on_timeout=True, rust_timeout=50)
            if watch_calls == 2:
                # Before successor registration; an overlapping old observer
                # must retain changes OUTSIDE the incoming subtree as well.
                inject_changes()
            async with aclosing(awatch(*args, **kwargs)) as observer:
                async for changes in observer:
                    ready.set()
                    yield changes

        def apply(vault, changes):
            if force_polling and not injected and (Change.added, str(directory)) in changes:
                # Polling retains its original snapshot at this same topology
                # boundary, and must detect mutations on the following cycle.
                inject_changes()
            result = real_apply(vault, changes)
            for _, value in changes:
                if value == str(directory):
                    loop.call_soon_threadsafe(directory_seen.set)
            rows = {row["path"]: row for row in self.entries()}
            if mutate_siblings:
                existing = rows.get(f"{sibling_dir.name}/existing.jpg", {})
                deleted = rows.get(f"{sibling_dir.name}/deleted.jpg", {})
                renamed = rows.get(f"{sibling_dir.name}/before-rename.jpg", {})
                after = rows.get(f"{sibling_dir.name}/after-rename.jpg", {})
                if (existing.get("size") == len(b"changed sibling")
                        and existing.get("plaintext_sha256") is None
                        and deleted.get("presence") == renamed.get("presence") == "missing"
                        and after.get("presence") == "present"):
                    loop.call_soon_threadsafe(siblings_seen.set)
            if target.relative_to(self.root).as_posix() in rows:
                loop.call_soon_threadsafe(file_seen.set)
            if later.relative_to(self.root).as_posix() in rows:
                loop.call_soon_threadsafe(later_seen.set)
            return result

        with (patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=force_polling)),
              patch("app.storage._runtime_settings", return_value=SimpleNamespace(filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=300 if force_polling else 30)),
              patch("app.storage.awatch", side_effect=ticking_watch),
              patch("app.storage.apply_filesystem_changes", side_effect=apply),
              # The scan paired with every watcher start has its own tests.
              patch("app.storage.scan_vault", return_value={})):
            task = asyncio.create_task(storage._watch_vault_filesystem(self.vault))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                target.parent.mkdir(parents=True)
                await asyncio.wait_for(directory_seen.wait(), 5)
                await asyncio.wait_for(file_seen.wait(), 5)
                if mutate_siblings:
                    await asyncio.wait_for(siblings_seen.wait(), 5)
                later.write_bytes(b"later")
                os.utime(later, (1_500_000_000, 1_500_000_000))
                # A subsequent native event must still arrive, not just the
                # one-time catch-up scan. Use a separate completion barrier.
                await asyncio.wait_for(later_seen.wait(), 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        rows = {row["path"]: row for row in self.entries()}
        if force_polling:
            self.assertEqual(watch_calls, 1)
        else:
            self.assertGreaterEqual(watch_calls, 2)
        self.assertEqual(rows[target.relative_to(self.root).as_posix()]["presence"], "present")
