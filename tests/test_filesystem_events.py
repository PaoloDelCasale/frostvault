"""Filesystem regressions with real local files and no database/network."""
from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import ExitStack, aclosing
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchfiles import Change, awatch

from app import storage
from tests import test_local_release


class ReleaseWatcherRaceTests(unittest.TestCase):
    setUp = test_local_release.LocalReleaseWorkerTests.setUp

    # Deliberately drive the production observer inside the hash, rather than
    # relying on file size, sleeps or the kernel's scheduling of a large hash.
    def test_watcher_during_hash_does_not_invalidate_local_release(self):
        real_hash = storage.hash_stable_regular_file
        self.catalog.mark_local_path_missing.side_effect = lambda **_: self.target.update(local_presence="missing")
        access = SimpleNamespace(local_operations_allowed=True, volume_alias=None)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None

        threads = []
        def observe_while_hashing(path):
            started = threading.Event()
            def watch():
                started.set()
                storage.apply_filesystem_changes(self.job, {(Change.deleted, str(self.path))})
            thread = threading.Thread(target=watch)
            threads.append(thread)
            thread.start()
            self.assertTrue(started.wait(2))
            # On the broken path, deterministically finish the watcher before
            # the hash. With serialization it must wait for catalog publication.
            if not storage.scan_lock_for_vault(self.job["vault_id"]).locked():
                thread.join(2)
                self.assertFalse(thread.is_alive())
            return real_hash(path)

        with (patch("app.storage.hash_stable_regular_file", side_effect=observe_while_hashing),
              patch("app.storage.source_layout.vault_local_access", return_value=access),
              patch("app.storage._validate_enrolled_scan_root"),
              patch("app.storage.record_catalog_revision")):
            try:
                storage.process_free_space(self.job)
            finally:
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
        self.assertFalse(self.path.exists())


    def test_failed_release_preserves_salvage_and_observes_external_replacement(self):
        real_hash = storage.hash_stable_regular_file
        def replace_then_fail(path):
            if path != self.path:
                self.path.write_bytes(b"other")
                raise RuntimeError("Injected hash failure")
            return real_hash(path)
        with patch("app.storage.hash_stable_regular_file", side_effect=replace_then_fail):
            with self.assertRaisesRegex(RuntimeError, "original content was preserved"):
                storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"other")
        claims = list(self.path.parent.glob(".*.cleanup-*.tmp"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].read_bytes(), b"photo")
        self.catalog.observe_replaced_local_copy.assert_called_once()

    def test_one_gib_file_with_active_watcher_is_released(self):
        size = 1 << 30
        with self.path.open("wb") as stream:
            stream.truncate(size)  # Sparse, disposable file; no media or real data.
        expected = hashlib.sha256()
        zeroes = b"\x00" * (1 << 20)
        for _ in range(1024):
            expected.update(zeroes)
        self.target.update(local_size=size, cloud_size=size, local_sha256=expected.hexdigest(), version_sha256=expected.hexdigest())
        self.target["local_mtime_ns"] = self.path.stat().st_mtime_ns
        self.head.return_value = {"ContentLength": size, "VersionId": "exact"}
        self.test_watcher_during_hash_does_not_invalidate_local_release()


class LocalObservationEchoTests(unittest.TestCase):
    def test_external_same_size_and_mtime_edit_is_not_suppressed(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"first")
            observed = path.stat()
            local_observation.remember(123, path.name, observed, hashlib.sha256(b"first").hexdigest())
            path.write_bytes(b"other")
            os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns))
            self.assertFalse(local_observation.consume(123, path.name, path))

    def test_echo_is_exact_one_shot_and_does_not_restat_later_content(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            local_observation.remember(124, path.name, path.stat(), hashlib.sha256(b"photo").hexdigest())
            self.assertTrue(local_observation.consume(124, path.name, path))
            self.assertFalse(local_observation.consume(124, path.name, path))


class LiveWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_files_copied_after_new_directory_are_observed(self):
        await self.check_new_directory(False)

    async def test_polling_files_copied_after_new_directory_are_observed(self):
        await self.check_new_directory(True)

    async def test_native_sibling_changes_during_rearm_are_not_lost(self):
        await self.check_new_directory(False, mutate_siblings=True)

    async def test_polling_sibling_changes_during_rearm_are_not_lost(self):
        await self.check_new_directory(True, mutate_siblings=True)

    async def check_new_directory(self, force_polling, *, mutate_siblings=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            ready = asyncio.Event()
            rearmed = asyncio.Event()
            watch_calls = 0
            active_observers = 0
            peak_observers = 0
            directory_seen = asyncio.Event()
            file_seen = asyncio.Event()
            events = []
            real_awatch = awatch
            siblings_seen = asyncio.Event()
            for name in ("existing.jpg", "deleted.jpg", "before-rename.jpg"):
                (root / name).write_bytes(b"first")
            expected = {
                (Change.modified, str(root / "existing.jpg")),
                (Change.deleted, str(root / "deleted.jpg")),
                (Change.deleted, str(root / "before-rename.jpg")),
                (Change.added, str(root / "after-rename.jpg")),
            }

            async def ticking_watch(*args, **kwargs):
                # A timeout tick proves the native observer is installed. It
                # also removes timing guesses from the newly-created-dir test.
                nonlocal watch_calls, active_observers, peak_observers
                watch_calls += 1
                active_observers += 1
                peak_observers = max(peak_observers, active_observers)
                if watch_calls == 2 and mutate_siblings:
                    # At successor construction: old implementation has already
                    # closed the observer. An overlapping handoff must retain
                    # these unrelated writes/deletions/renames in the old one.
                    original = (root / "existing.jpg").stat()
                    (root / "existing.jpg").write_bytes(b"modified sibling bytes")
                    # notify's polling backend compares whole-second mtimes.
                    os.utime(root / "existing.jpg", ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
                    (root / "deleted.jpg").unlink()
                    (root / "before-rename.jpg").rename(root / "after-rename.jpg")
                kwargs.update(yield_on_timeout=True, rust_timeout=50)
                try:
                    async with aclosing(real_awatch(*args, **kwargs)) as observer:
                        async for changes in observer:
                            ready.set()
                            if watch_calls > 1:
                                rearmed.set()
                            yield changes
                finally:
                    active_observers -= 1

            def apply(vault, changes):
                events.extend(changes)
                if expected <= set(events):
                    siblings_seen.set()
                for _, value in changes:
                    if value == str(root / "new"):
                        directory_seen.set()
                    if value == str(root / "new" / "nested" / "later.bin"):
                        file_seen.set()

            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True)))
            stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=30)))
            stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=force_polling)))
            stack.enter_context(patch("app.storage.awatch", side_effect=ticking_watch))
            # Keep callbacks on the event-loop thread for asyncio.Event safety.
            async def direct(function, *args, **kwargs):
                return function(*args, **kwargs)
            stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=direct))
            stack.enter_context(patch("app.storage.apply_filesystem_changes", side_effect=apply))
            task = asyncio.create_task(storage._watch_vault_filesystem({"id": 2, "source_root": directory}))
            try:
                await asyncio.wait_for(ready.wait(), 3)
                (root / "new" / "nested").mkdir(parents=True)
                await asyncio.wait_for(directory_seen.wait(), 3)
                await asyncio.wait_for(rearmed.wait(), 3)
                if mutate_siblings:
                    try:
                        await asyncio.wait_for(siblings_seen.wait(), 3)
                    except TimeoutError:
                        self.fail(f"Missing sibling events: {expected - set(events)}; got {events}")
                (root / "new" / "nested" / "later.bin").write_bytes(b"later")
                await asyncio.wait_for(file_seen.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(events)
            self.assertEqual(active_observers, 0, "shutdown leaked a recursive observer")
            self.assertEqual(peak_observers, 2, "handoff must overlap exactly two observers")


class WatcherSubtreeTests(unittest.TestCase):
    def test_move_in_emits_only_directory_event_but_catalogues_nested_files(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            root = base / "vault"
            root.mkdir()
            incoming = base / "incoming"
            (incoming / "nested").mkdir(parents=True)
            (incoming / "nested" / "file.bin").write_bytes(b"data")
            incoming.rename(root / "incoming")
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            observed = []
            catalog.observe_local_copy.side_effect = lambda **kwargs: observed.append(kwargs["path"])
            catalog.observe_local_copies_batch.side_effect = lambda **kwargs: observed.extend(entry[0] for entry in kwargs["entries"])
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True, volume_alias=None)))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            storage.apply_filesystem_changes({"id": 2, "source_root": str(root)}, {(Change.added, str(root / "incoming"))})
            self.assertIn("incoming/nested/file.bin", observed)
            catalog.observe_local_copies_batch.reset_mock()
            self.assertEqual(storage.apply_filesystem_changes(
                {"id": 2, "source_root": str(root)}, {(Change.modified, str(root / "incoming"))},
            ), 0)
            catalog.observe_local_copies_batch.assert_not_called()


class SubtreeContainmentTests(unittest.TestCase):
    def check_swap(self, *, during_walk=False, nested=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            root = base / "vault"
            incoming = root / "parent" / "incoming" if nested else root / "incoming"
            incoming.mkdir(parents=True)
            for name in ("a.jpg", "b.jpg", "c.jpg"):
                (incoming / name).write_bytes(b"inside")
            outside = base / "outside"
            outside.mkdir()
            (outside / "outside-only.txt").write_bytes(b"private")
            if nested:
                (outside / "incoming").mkdir()
                (outside / "incoming" / "outside-only.txt").write_bytes(b"private")
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            observed = []
            catalog.observe_local_copies_batch.side_effect = lambda **kwargs: observed.extend(entry[0] for entry in kwargs["entries"])
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True, volume_alias=None)))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            rollback = stack.enter_context(patch("app.storage._rollback_local_scan_journal"))
            stack.enter_context(patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 1))
            real_scan, real_inspect, real_read = storage._scan_tree, storage.inspect_entry, os.read
            read_outside = []
            inspections = 0

            def swap():
                target = incoming.parent if nested else incoming
                target.rename(base / "detached")
                target.symlink_to(outside, target_is_directory=True)

            def scan(*args, **kwargs):
                if not during_walk:
                    swap()  # After is_symlink(), immediately before real traversal.
                return real_scan(*args, **kwargs)

            def inspect(path):
                nonlocal inspections
                result = real_inspect(path)
                if result and result[0] == "regular":
                    inspections += 1
                    if during_walk and inspections == 2:
                        swap()  # First bounded batch has already committed.
                return result

            def read(fd, count):
                if Path(os.readlink(f"/proc/self/fd/{fd}")).is_relative_to(outside):
                    read_outside.append(True)
                return real_read(fd, count)

            with (patch("app.storage._scan_tree", side_effect=scan),
                  patch("app.storage.inspect_entry", side_effect=inspect),
                  patch("os.read", side_effect=read)):
                error = None
                try:
                    storage.apply_filesystem_changes({"id": 2, "source_root": str(root)}, {(Change.added, str(incoming))})
                except RuntimeError as exc:
                    error = exc
            self.assertEqual(read_outside, [], "read bytes outside the enrolled Vault")
            self.assertFalse(any("outside-only" in path for path in observed), observed)
            self.assertIsNotNone(error, "substituted subtree must fail closed")
            rollback.assert_called_once()
            if during_walk:
                self.assertGreaterEqual(connection.commit.call_count, 1)

    def test_subtree_swapped_to_symlink_before_traversal(self):
        self.check_swap()

    def test_ancestor_swapped_to_symlink_before_traversal(self):
        self.check_swap(nested=True)

    def test_subtree_swapped_during_traversal_after_committed_batch(self):
        self.check_swap(during_walk=True)

    def test_ancestor_swapped_during_traversal_after_committed_batch(self):
        self.check_swap(during_walk=True, nested=True)


class ReleaseRepairVaultIdentityTests(unittest.TestCase):
    setUp = test_local_release.LocalReleaseWorkerTests.setUp

    def test_repair_uses_vault_id_not_unrelated_same_number_job_id(self):
        self.job.update(id=100, vault_id=2)
        vault = {"id": 2, "source_root": self.job["source_root"]}
        unrelated = {"id": 100, "source_root": "/unrelated"}
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.side_effect = lambda sql, params: SimpleNamespace(fetchone=lambda: vault if params == (2,) else unrelated)
        with (patch("app.storage._validate_enrolled_scan_root") as validate,
              patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True))):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        validate.assert_called_with(vault)
        self.catalog.set_local_fingerprint.assert_called_once()

    def test_missing_actual_vault_never_falls_back_to_unenrolled_job(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None
        with (patch("app.storage.inspect_entry") as inspect,
              self.assertRaises(RuntimeError)):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()

    def test_actual_vault_identity_change_fails_before_read(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = {
            "id": 2, "source_root": self.job["source_root"],
            "root_identity": "replaced", "root_identity_version": storage.vault_relocation.ROOT_IDENTITY_VERSION,
        }
        with (patch("app.storage.inspect_entry") as inspect,
              self.assertRaisesRegex(RuntimeError, "identity mismatch")):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()

    def test_actual_vault_unavailable_source_fails_before_read(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = {"id": 2, "source_root": self.job["source_root"]}
        with (patch("app.storage.inspect_entry") as inspect,
              patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=False)),
              self.assertRaisesRegex(RuntimeError, "Source Volume changed")):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()

    def test_changed_actual_vault_fails_before_read_or_publication(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = {"id": 2, "source_root": "/changed"}
        with (patch("app.storage.inspect_entry") as inspect,
              self.assertRaisesRegex(RuntimeError, "root changed")):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()


class SDKTemporaryNameTests(unittest.TestCase):
    def test_managed_download_suffix_is_reserved_only_on_our_generated_names(self):
        for operation in ("restore", "verify"):
            name = f".photo.jpg.{operation}-{'a' * 32}.tmp.f8Fc29bA"
            self.assertTrue(storage.is_restore_temporary_name(name), name)
            self.assertFalse(storage._filesystem_watch_filter(Change.added, name))
        for name in ("photo.jpg.tmp.f8Fc29bA", "notes.tmp.12345678", ".user.restore-not-a-uuid.tmp.f8Fc29bA"):
            self.assertFalse(storage.is_restore_temporary_name(name), name)
            self.assertTrue(storage._filesystem_watch_filter(Change.added, name))
