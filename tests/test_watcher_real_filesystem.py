"""The Vault watcher against a real directory tree and real native observers.

These tests use a temporary directory and watchfiles' own observers: what the
native registration does with unreadable or undecodable folders is exactly what
the mocked tests cannot tell. Permission cases need an unprivileged user (root
ignores permission bits); they are skipped, with the reason, when run as root.
No database is involved: the scan and the batch application are replaced by
small stand-ins that report what a real scan would record.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest.mock
from unittest.mock import patch

from watchfiles import Change, awatch

from app import storage
from app.services import watch_plan
from app.services.fs_preflight import PinnedTree, is_storable_name

NOT_ROOT = unittest.skipIf(
    os.geteuid() == 0,
    "root ignores permission bits, so an unreadable folder cannot be simulated; "
    "run as an unprivileged user (CI does)",
)
VAULT_ID = 9461


def observer_options():
    return dict(
        debounce=30, yield_on_timeout=True, rust_timeout=200,
        ignore_permission_denied=True,
    )


def survey(root: Path) -> watch_plan.UnobservedPaths:
    """What a full scan would record: unreadable folders and unsupported names."""
    unreadable: set[str] = set()
    unsupported: set[str] = set()
    directories: set[str] = set()

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            is_directory = entry.is_dir(follow_symlinks=False)
            if not is_storable_name(entry.name):
                unsupported.add(relative)
                if is_directory:
                    directories.add(relative)
                continue
            if not is_directory:
                continue
            try:
                os.scandir(entry.path).close()
            except OSError:
                unreadable.add(relative)
                directories.add(relative)
                continue
            walk(Path(entry.path), relative + "/")

    walk(root, "")
    return watch_plan.UnobservedPaths(
        frozenset(unreadable), frozenset(unsupported), frozenset(directories),
    )


def lock_down(path: Path) -> None:
    path.mkdir(exist_ok=True)
    path.chmod(0o000)


def open_up(root: Path) -> None:
    for directory, names, _files in os.walk(root):
        os.chmod(directory, 0o755)
        for name in names:
            try:
                os.chmod(os.path.join(directory, name), 0o755)
            except OSError:
                pass


class RealTreeCase(unittest.IsolatedAsyncioTestCase):
    def make_root(self) -> Path:
        directory = tempfile.mkdtemp(prefix="frostvault-watch-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.addCleanup(open_up, Path(directory))
        return Path(directory).resolve()

    async def drain(self, observer, seconds: float) -> set[str]:
        """Collect the paths an observer reports during ``seconds``."""
        seen: set[str] = set()
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                changes = await asyncio.wait_for(anext(observer), 2)
            except asyncio.TimeoutError:
                break
            seen |= {path for _change, path in changes}
        return seen


@NOT_ROOT
class WatchPlanAroundUnreadableFoldersTests(RealTreeCase):
    """Review bug 1: the siblings of an unreadable folder stay watched."""

    async def watch_plan_observer(self, root: Path, found: watch_plan.UnobservedPaths):
        flat, recursive = watch_plan.watch_directories(root, found)
        observers = []
        if flat:
            observers.append(awatch(*flat, recursive=False, **observer_options()))
        if recursive:
            observers.append(awatch(*recursive, recursive=True, **observer_options()))
        merged = watch_plan.MergedWatch(observers)
        await asyncio.wait_for(anext(merged), 5)  # registration barrier
        return merged

    async def test_every_readable_sibling_is_watched_wherever_the_unreadable_one_is_listed(self):
        root = self.make_root()
        # Several unreadable names: whichever the filesystem lists first, the
        # native registration used to stop there and leave the rest unwatched.
        for name in ("0denied", "mdenied", "zdenied"):
            lock_down(root / name)
        for name in ("a1", "a2", "a3"):
            (root / name / "deep").mkdir(parents=True)
        found = survey(root)
        self.assertEqual(found.unreadable, {"0denied", "mdenied", "zdenied"})
        observer = await self.watch_plan_observer(root, found)
        try:
            targets = [root / "a1" / "deep" / "x.txt", root / "a2" / "y.txt", root / "a3" / "z.txt", root / "top.txt"]
            for target in targets:
                target.write_text("x")
            seen = await self.drain(observer, 1.5)
        finally:
            await observer.aclose()
        self.assertTrue({str(t) for t in targets} <= seen, sorted(seen))

    async def test_nested_unreadable_folder_keeps_its_cousins_watched(self):
        root = self.make_root()
        (root / "a" / "b").mkdir(parents=True)
        lock_down(root / "a" / "b" / "denied")
        (root / "a" / "b" / "ok").mkdir()
        (root / "a" / "c").mkdir()
        (root / "d").mkdir()
        found = survey(root)
        self.assertEqual(found.unreadable, {"a/b/denied"})
        observer = await self.watch_plan_observer(root, found)
        try:
            targets = [root / "a" / "b" / "ok" / "1", root / "a" / "c" / "2", root / "d" / "3", root / "a" / "b" / "4", root / "a" / "5"]
            for target in targets:
                target.write_text("x")
            seen = await self.drain(observer, 1.5)
        finally:
            await observer.aclose()
        self.assertTrue({str(t) for t in targets} <= seen, sorted(seen))

    async def test_a_new_subfolder_next_to_the_gap_is_announced(self):
        root = self.make_root()
        lock_down(root / "denied")
        (root / "a").mkdir()
        observer = await self.watch_plan_observer(root, survey(root))
        try:
            (root / "fresh").mkdir()
            seen = await self.drain(observer, 1.2)
        finally:
            await observer.aclose()
        self.assertIn(str(root / "fresh"), seen)


class Harness(RealTreeCase):
    """Run the real ``_watch_vault_filesystem`` with a stand-in scan and batch."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.applied: list[set[tuple[Change, str]]] = []
        self.registrations: list[tuple[str, ...]] = []
        self.ready_registrations: list[tuple[str, ...]] = []
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None
        self.stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True)))
        self.stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=30)))
        self.stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=False)))
        self.stack.enter_context(patch("app.storage.WATCHER_RESTART_BASE_SECONDS", 0.05))
        self.stack.enter_context(patch("app.storage.WATCHER_GAP_RESCAN_MIN_SECONDS", 0.05))
        self.stack.enter_context(patch("app.storage.WATCHER_RECHECK_SECONDS", 0.1))
        self.resolve = self.stack.enter_context(patch("app.storage._resolve_worker_error"))
        self.report = self.stack.enter_context(patch("app.storage._report_worker_error"))
        self.health = self.stack.enter_context(patch("app.storage._record_scan_component_health"))
        self.stack.enter_context(patch("app.storage.worker_error_store.log_worker_error"))
        self.stack.enter_context(patch("app.storage._apply_watcher_changes", side_effect=lambda vault, changes: self.applied.append(set(changes)) or 1))
        real_awatch = awatch

        def counting_awatch(*paths, **kwargs):
            self.registrations.append(tuple(str(p) for p in paths))
            async def registered():
                observer = real_awatch(*paths, **kwargs)
                try:
                    first = await anext(observer)
                    self.ready_registrations.append(tuple(str(p) for p in paths))
                    yield first
                    async for changes in observer:
                        yield changes
                finally:
                    await observer.aclose()
            return registered()

        self.stack.enter_context(patch("app.storage.awatch", side_effect=counting_awatch))
        self.addCleanup(watch_plan.forget, VAULT_ID)
        self.addCleanup(storage.runtime_status.pop, VAULT_ID, None)

    def scanning(self, root: Path, *, outcome=None):
        def scan(vault):
            found = survey(root)
            watch_plan.record_full_scan(VAULT_ID, found)
            if outcome is not None:
                return outcome(found)
            return {"local": 1, "local_incomplete": 1} if found else {"local": 1, "cloud": 1}

        return self.stack.enter_context(patch("app.storage.scan_vault", side_effect=scan))

    def start(self, root: Path):
        return asyncio.create_task(storage._watch_vault_filesystem({"id": VAULT_ID, "source_root": str(root)}))

    async def settle(self, condition, timeout=8):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            self.assertLess(asyncio.get_running_loop().time(), deadline, "condition not reached")
            await asyncio.sleep(0.02)

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def applied_paths(self) -> set[str]:
        return {path for batch in self.applied for _change, path in batch}


@NOT_ROOT
class UnreadableFolderLifecycleTests(Harness):
    async def test_watcher_registers_around_the_gap_then_the_whole_tree_after_the_repair(self):
        root = self.make_root()
        for name in ("0denied", "a1", "a2"):
            (root / name).mkdir()
        (root / "0denied").chmod(0o000)
        scan = self.scanning(root)
        task = self.start(root)
        try:
            # The scan names the gap; the watcher registers the readable rest.
            await self.settle(lambda: scan.call_count >= 1 and len(self.registrations) >= 3)
            self.assertEqual(self.registrations[0], (str(root),))
            (root / "a1" / "one.txt").write_text("x")
            (root / "a2" / "two.txt").write_text("x")
            await self.settle(lambda: {str(root / "a1" / "one.txt"), str(root / "a2" / "two.txt")} <= self.applied_paths())
            # Repair the permission: only the reported path is looked at, then
            # the Vault is scanned and the tree registered again as a whole.
            (root / "0denied").chmod(0o755)
            await self.settle(lambda: scan.call_count >= 2)
            await self.settle(lambda: self.registrations[-1] == (str(root),))
            (root / "0denied" / "late.txt").write_text("x")
            await self.settle(lambda: str(root / "0denied" / "late.txt") in self.applied_paths())
        finally:
            await self.stop(task)

    async def test_a_gap_found_later_by_a_scan_rearms_the_observer(self):
        root = self.make_root()
        for name in ("a1", "a2"):
            (root / name).mkdir()
        scan = self.scanning(root)
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1)
            registrations = len(self.registrations)
            lock_down(root / "a1" / "late-denied")
            # The cheap re-check only runs for known problems, so a later full
            # scan (here: recorded by hand) is what teaches the watcher.
            watch_plan.record_full_scan(VAULT_ID, survey(root))
            await self.settle(lambda: len(self.registrations) > registrations)
            (root / "a2" / "after.txt").write_text("x")
            await self.settle(lambda: str(root / "a2" / "after.txt") in self.applied_paths())
        finally:
            await self.stop(task)


@NOT_ROOT
class PermissionRecoveryBoundaryTests(Harness):
    """#506: write at scan_vault's actual source-health publication boundary."""

    async def recovery_boundary(self, *, polling):
        root = self.make_root()
        repaired = root / "denied"
        (repaired / "deep").mkdir(parents=True)
        (root / "sibling").mkdir()
        repaired.chmod(0o000)
        self.stack.enter_context(patch(
            "app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=polling),
        ))
        vault = {"id": VAULT_ID, "source_root": str(root), "s3_bucket": "test"}
        scans = []
        walked = []
        confirmed = threading.Event()
        before_registration = repaired / "deep" / "before-registration.txt"
        target = repaired / "deep" / "first-at-confirmation.txt"

        def local_scan(_vault, _scan_id):
            found = survey(root)
            watch_plan.record_full_scan(VAULT_ID, found)
            scans.append(found)
            walked.append({str(Path(directory) / name)
                           for directory, _folders, names in os.walk(root) for name in names})
            return 0, {}, None, not bool(found)

        def health(_vault_id, component, *, ok, **kwargs):
            if component != "source":
                return
            if kwargs.get("error") == "Source watcher registration pending":
                before_registration.write_text("missed by the first recovery walk")
            if ok and not confirmed.is_set():
                # This is after the local walk, not after a convenient sleep or
                # an observed re-registration. No later writes can rescue it.
                self.assertEqual(self.ready_registrations[-1], (str(root),),
                                 "the repaired subtree must already be recursively watched")
                self.assertIn(str(before_registration), walked[-1],
                              "the paired walk must catch files predating registration")
                target.write_text("created at the health-clear boundary")
                confirmed.set()

        self.health.side_effect = health
        self.stack.enter_context(patch("app.storage._current_scan_vault", return_value=vault))
        self.stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
        self.stack.enter_context(patch("app.storage._verified_local_scan", side_effect=local_scan))
        self.stack.enter_context(patch("app.storage.scan_cloud", return_value=0))
        self.stack.enter_context(patch("app.storage.validate_cloud_vault"))
        self.stack.enter_context(patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0))
        self.stack.enter_context(patch("app.storage.s3_client"))
        self.stack.enter_context(patch("app.storage.owner_user_id", return_value=None))
        self.stack.enter_context(patch("app.storage.record_catalog_revision"))
        task = self.start(root)
        try:
            await self.settle(lambda: scans and (str(root / "sibling"),) in self.ready_registrations)
            # Prove the old plan is live, not merely constructed.
            sibling = root / "sibling" / "still-watched.txt"
            sibling.write_text("sibling")
            await self.settle(lambda: str(sibling) in self.applied_paths())
            repaired.chmod(0o755)
            await self.settle(confirmed.is_set)
            scans_at_confirmation = len(scans)
            await self.settle(lambda: str(target) in self.applied_paths(), timeout=4)
            self.assertEqual(len(scans), scans_at_confirmation,
                             "the boundary file must arrive by event, not a later catch-up scan")
        finally:
            await self.stop(task)

    async def test_native_first_file_at_source_confirmation_is_observed(self):
        await self.recovery_boundary(polling=False)

    async def test_polling_first_file_at_source_confirmation_is_observed(self):
        await self.recovery_boundary(polling=True)


class FolderCreatedUnreadableDuringTheWatchTests(Harness):
    """Review bug 2, end to end: the #495 ``mkdir`` while the watcher runs."""

    def subtree_scanning_apply(self, root: Path):
        """Stand-in batch that records a new folder as a real subtree walk does."""
        def apply(vault, changes):
            self.applied.append(set(changes))
            for change, value in changes:
                path = Path(value)
                if change != Change.added or not path.is_dir():
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    os.scandir(path).close()
                    found = watch_plan.UnobservedPaths()
                except PermissionError:
                    found = watch_plan.UnobservedPaths(
                        frozenset({relative}), frozenset(), frozenset({relative}),
                    )
                watch_plan.record_subtree_scan(VAULT_ID, relative, found)
            return 1

        self.stack.enter_context(patch("app.storage._apply_watcher_changes", side_effect=apply))

    async def run_lifecycle(self, root: Path):
        (root / "a").mkdir()
        scan = self.scanning(root)
        self.subtree_scanning_apply(root)
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1 and self.resolve.call_count >= 1)
            registrations = len(self.registrations)
            # As another user (root) would create it: not enterable.
            os.mkdir(root / "late", 0o000)
            await self.settle(lambda: watch_plan.unobserved(VAULT_ID).directories == {"late"})
            await self.settle(lambda: len(self.registrations) > registrations)
            (root / "a" / "still.txt").write_text("x")
            await self.settle(lambda: str(root / "a" / "still.txt") in self.applied_paths())
            # Repairing it raises no event: the re-check of the named path
            # notices, the Vault is scanned and the tree registered as a whole.
            scans = scan.call_count
            (root / "late").chmod(0o755)
            await self.settle(lambda: scan.call_count > scans)
            await self.settle(lambda: self.registrations[-1] == (str(root),))
            (root / "late" / "inside.txt").write_text("x")
            await self.settle(lambda: str(root / "late" / "inside.txt") in self.applied_paths())
        finally:
            await self.stop(task)

    @NOT_ROOT
    async def test_native_observer_registers_around_it_and_again_after_the_repair(self):
        await self.run_lifecycle(self.make_root())
        self.report.assert_not_called()

    @NOT_ROOT
    async def test_polling_observer_that_stops_on_it_restarts_around_it(self):
        self.stack.enter_context(patch(
            "app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=True),
        ))
        await self.run_lifecycle(self.make_root())
        # Not an internal error: the path is the problem, and a scan names it.
        self.report.assert_not_called()
        errors = [c.kwargs.get("error", "") for c in self.health.call_args_list if not c.kwargs.get("ok", True)]
        self.assertIn("Source watcher stopped on a folder it may not read", errors)


class RemovalOnlyWhenProvenTests(RealTreeCase):
    """Review bug 5 in the watcher path: a rename to a name that cannot be
    catalogued is not a removal."""

    def apply(self, root: Path, changes):
        catalog = SimpleNamespace(mark_local_path_missing=unittest.mock.Mock(), observe_local_copy=unittest.mock.Mock())
        with ExitStack() as stack:
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.ArchiveCatalog", return_value=catalog))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            storage.apply_filesystem_changes({"id": VAULT_ID, "source_root": str(root)}, changes)
        return [call.kwargs["path"] for call in catalog.mark_local_path_missing.call_args_list]

    def test_a_folder_renamed_to_an_undecodable_name_is_not_missing(self):
        root = self.make_root()
        (root / "Foto").mkdir()
        (root / "Foto" / "a.jpg").write_text("x")
        os.rename(os.fsencode(root / "Foto"), os.fsencode(root) + b"/Fot\xf2")
        removed = self.apply(root, {(Change.deleted, str(root / "Foto")), (Change.deleted, str(root / "Foto" / "a.jpg"))})
        self.assertEqual(removed, [])

    def test_a_file_renamed_to_an_undecodable_name_deep_down_is_not_missing(self):
        root = self.make_root()
        (root / "x" / "y").mkdir(parents=True)
        (root / "x" / "y" / "doc.txt").write_text("x")
        os.rename(os.fsencode(root / "x" / "y" / "doc.txt"), os.fsencode(root / "x" / "y") + b"/d\xf2c.txt")
        self.assertEqual(self.apply(root, {(Change.deleted, str(root / "x" / "y" / "doc.txt"))}), [])

    def test_a_real_removal_is_still_missing(self):
        root = self.make_root()
        (root / "keep").mkdir()
        (root / "gone").mkdir()
        (root / "gone" / "a.txt").write_text("x")
        (root / "file.txt").write_text("x")
        shutil.rmtree(root / "gone")
        os.unlink(root / "file.txt")
        removed = self.apply(root, {
            (Change.deleted, str(root / "gone" / "a.txt")),
            (Change.deleted, str(root / "gone")),
            (Change.deleted, str(root / "file.txt")),
        })
        self.assertEqual(sorted(removed), ["file.txt", "gone", "gone/a.txt"])

    def test_an_undecodable_name_elsewhere_does_not_hide_a_removal(self):
        root = self.make_root()
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "b" / "old.txt").write_text("x")
        os.mkdir(os.fsencode(root / "a") + b"/Fot\xf2")
        os.unlink(root / "b" / "old.txt")
        self.assertEqual(self.apply(root, {(Change.deleted, str(root / "b" / "old.txt"))}), ["b/old.txt"])

    @NOT_ROOT
    def test_a_removal_below_a_folder_that_cannot_be_listed_is_not_proven(self):
        root = self.make_root()
        (root / "locked").mkdir()
        (root / "locked").chmod(0o300)  # enterable, not listable
        self.assertEqual(self.apply(root, {(Change.deleted, str(root / "locked" / "gone.txt"))}), [])


class UndecodableDirectoryTests(Harness):
    """Review bug 3: a non-UTF-8 directory used to keep the watcher from starting."""

    async def test_native_registration_refuses_an_existing_undecodable_directory(self):
        root = self.make_root()
        os.mkdir(os.fsencode(root) + b"/Fot\xf2")
        observer = awatch(root, recursive=True, **observer_options())
        try:
            with self.assertRaises(BaseException) as raised:
                await asyncio.wait_for(anext(observer), 5)
        finally:
            await observer.aclose()
        self.assertTrue(storage._is_undecodable_path_error(raised.exception), repr(raised.exception))

    async def test_watcher_starts_anyway_around_it_and_keeps_the_vault_unconfirmed(self):
        root = self.make_root()
        (root / "ok").mkdir()
        os.mkdir(os.fsencode(root) + b"/Fot\xf2")
        scan = self.scanning(root)
        task = self.start(root)
        try:
            await self.settle(lambda: self.resolve.call_count >= 1)
            self.assertGreaterEqual(scan.call_count, 1, "the failed start is followed by a scan that names the path")
            (root / "ok" / "new.txt").write_text("x")
            await self.settle(lambda: str(root / "ok" / "new.txt") in self.applied_paths())
        finally:
            await self.stop(task)
        errors = [c.kwargs for c in self.health.call_args_list if not c.kwargs.get("ok", True)]
        self.assertTrue(errors and "UTF-8" in errors[0]["error"], errors)
        self.report.assert_not_called()

    async def test_a_burst_of_undecodable_names_is_not_a_burst_of_scans(self):
        root = self.make_root()
        (root / "ok").mkdir()
        scan = self.scanning(root)
        raw = os.fsencode(root) + b"/ok/"
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1)
            with patch("app.storage.WATCHER_GAP_RESCAN_MIN_SECONDS", 1.0):
                before_scans = scan.call_count
                before_registrations = len(self.registrations)
                for number in range(12):
                    with open(raw + b"bad\xf2" + str(number).encode(), "wb") as handle:
                        handle.write(b"x")
                    await asyncio.sleep(0.25)
            restarts = len(self.registrations) - before_registrations
        finally:
            await self.stop(task)
        scans = scan.call_count - before_scans
        self.assertGreaterEqual(restarts, 3, "the observer really stopped repeatedly")
        self.assertLessEqual(scans, 5, f"{scans} scans for {restarts} restarts in 3 s")
        self.assertLess(scans, restarts)


class MergedWatchTests(RealTreeCase):
    async def test_an_observer_failure_reaches_the_consumer_and_closing_is_clean(self):
        root = self.make_root()
        (root / "a").mkdir()
        os.mkdir(os.fsencode(root) + b"/a/Fot\xf2")
        merged = watch_plan.MergedWatch([
            awatch(root / "a", recursive=True, **observer_options()),
            awatch(root, recursive=False, **observer_options()),
        ])
        try:
            with self.assertRaises(BaseException) as raised:
                await asyncio.wait_for(anext(merged), 5)
            self.assertTrue(storage._is_undecodable_path_error(raised.exception))
        finally:
            await merged.aclose()
        self.assertEqual(
            [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()], [],
        )


class WatcherScanSerialisationTests(unittest.IsolatedAsyncioTestCase):
    """Review bug 7: watcher-paired scans of several Vaults take turns."""

    async def test_scans_started_together_never_run_together(self):
        running = 0
        peak = 0
        guard = threading.Lock()

        def scan(vault):
            nonlocal running, peak
            with guard:
                running += 1
                peak = max(peak, running)
            threading.Event().wait(0.05)
            with guard:
                running -= 1
            return {"local": 1, "cloud": 1}

        with patch("app.storage.scan_vault", side_effect=scan):
            outcomes = await asyncio.gather(*[
                storage._reconcile_vault_after_watcher({"id": number}) for number in range(1, 6)
            ])
        self.assertEqual(peak, 1)
        self.assertTrue(all(o.retry_after is None and not o.recheck for o in outcomes))


class WatcherFailureBookkeepingTests(Harness):
    """Review bugs 2 (batch that never applies) and 4 (backoff)."""

    async def test_a_batch_that_never_applies_is_dropped_and_the_vault_scanned(self):
        root = self.make_root()
        scan = self.scanning(root)
        attempts = []

        def broken(vault, changes):
            attempts.append(set(changes))
            if len(attempts) <= 3:
                raise RuntimeError("Subtree component could not be pinned")
            return 1

        self.stack.enter_context(patch("app.storage._apply_watcher_changes", side_effect=broken))
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1)
            (root / "new.txt").write_text("x")
            await self.settle(lambda: len(attempts) >= 3)
            await asyncio.sleep(0.6)
            self.assertEqual(len(attempts), 3, "the same batch is not retried forever")
            self.assertTrue(any(path == str(root / "new.txt") for _c, path in attempts[0]))
            # Later changes still go through, without the dropped batch.
            (root / "later.txt").write_text("x")
            await self.settle(lambda: len(attempts) >= 4)
            self.assertEqual({path for _c, path in attempts[3]}, {str(root / "later.txt")})
            self.assertGreaterEqual(scan.call_count, 2, "what the dropped batch held is found by a scan")
        finally:
            await self.stop(task)

    async def test_time_spent_scanning_does_not_reset_the_backoff(self):
        root = self.make_root()
        delays: list[int] = []
        self.stack.enter_context(patch(
            "app.storage._watcher_restart_delay", side_effect=lambda streak: delays.append(streak) or 0.02,
        ))
        self.stack.enter_context(patch("app.storage.WATCHER_STABLE_SECONDS", 0.15))
        scans = 0

        def slow_scan(vault):
            nonlocal scans
            scans += 1
            threading.Event().wait(0.3)
            return {"local": 1, "cloud": 1}

        self.stack.enter_context(patch("app.storage.scan_vault", side_effect=slow_scan))

        def dying(*paths, **kwargs):
            async def generate():
                yield set()
                await asyncio.sleep(0.05)  # far shorter than the stable time
                raise RuntimeError("native observer died")
            return generate()

        self.stack.enter_context(patch("app.storage.awatch", side_effect=dying))
        task = self.start(root)
        try:
            await self.settle(lambda: len(delays) >= 3, timeout=10)
        finally:
            await self.stop(task)
        self.assertEqual(delays[:3], [1, 2, 3], "a long scan is not the watcher staying up")


class PinnedSubtreeTests(RealTreeCase):
    """Review bug 2: the folder that appears unreadable during a watch."""

    @NOT_ROOT
    def test_an_unreadable_folder_as_scan_start_is_reported_not_an_identity_change(self):
        root = self.make_root()
        (root / "other").mkdir()
        lock_down(root / "created-by-someone-else")
        failures = []
        with PinnedTree(root, subtree=root / "created-by-someone-else") as tree:
            entries = list(tree.entries(on_error=lambda path, exc: failures.append((path, exc))))
            tree.validate()
        self.assertEqual(entries, [])
        self.assertEqual([path for path, _ in failures], [root / "created-by-someone-else"])
        self.assertIsInstance(failures[0][1], PermissionError)

    @NOT_ROOT
    def test_the_subtree_scan_of_an_unreadable_new_folder_names_only_that_path(self):
        """The exact #495 case: a folder created unreadable while the watcher runs."""
        root = self.make_root()
        (root / "fine").mkdir()
        (root / "fine" / "a.txt").write_text("a")
        lock_down(root / "created-by-someone-else")
        watch_plan.record_full_scan(VAULT_ID, watch_plan.UnobservedPaths(
            frozenset({"old-gap"}), frozenset(), frozenset({"old-gap"}),
        ))
        self.addCleanup(watch_plan.forget, VAULT_ID)
        problems = []
        journal = {}
        with ExitStack() as stack:
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.ArchiveCatalog"))
            stack.enter_context(patch("app.storage.report_problem", side_effect=lambda *a, **kw: problems.append((kw["event"], kw["path"]))))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            storage._scan_tree(
                connection, {"id": VAULT_ID, "source_root": str(root)}, "event", root=root,
                subtree=root / "created-by-someone-else", allow_chunk_commits=False,
                finalize_missing=False, scan_journal=journal,
            )
        self.assertEqual(problems, [("file_unreadable", "created-by-someone-else")])
        self.assertTrue(journal["incomplete"])
        known = watch_plan.unobserved(VAULT_ID)
        # The partial walk only speaks for its own subtree.
        self.assertEqual(known.directories, {"old-gap", "created-by-someone-else"})

    def test_a_vanished_start_folder_is_still_an_identity_change(self):
        from app.services.fs_preflight import TreeIdentityChanged
        root = self.make_root()
        with PinnedTree(root, subtree=root / "gone") as tree:
            with self.assertRaises(TreeIdentityChanged):
                list(tree.entries(on_error=lambda *_: None))


if __name__ == "__main__":
    unittest.main()
