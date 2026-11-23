"""Traversal of a full scan on a large adoption over FUSE (#696).

Seams under test:
1. The ancestor identity fence runs once per folder, not once per entry.
2. The readability probe is skipped for a file the verification hashes, and
   kept (open only, #709) for a fingerprinted file whose size and mtime are
   unchanged.
3. An unreadable file that skipped the probe is still a ``file_unreadable``
   Problem, makes the scan incomplete and is a gap the watcher re-checks.
4. The runtime status reports files seen, their rate and, after a previous
   scan, how many to expect.
5. The traversal times its wait for the Vault row apart from its batch writes
   and its walk, in the runtime status and in the log (#707).
6. A fingerprinted, unchanged file is only opened, never read, and the time
   the walk spends checking files is reported (#709).
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.services.fs_preflight import PinnedTree, inspect_entry
from app.services.vaults import VAULT_ROW_SQL
from tests.test_scan_fingerprints import _quiet_scan_patches
from tests.test_vault_adoption import _AdoptionFixture


def _deep_tree(directory: str, files: int) -> Path:
    root = Path(directory) / "vault"
    leaf = root / "library" / "user" / "2026" / "2026-11-15"
    leaf.mkdir(parents=True)
    for index in range(files):
        (leaf / f"img{index:03d}.jpg").write_bytes(b"x")
    return root


def _ancestor_stats(files: int) -> int:
    """How many ``stat`` calls the walk's identity fence makes."""
    real_stat = os.stat
    calls = {"n": 0}

    def counting(path, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            calls["n"] += 1
        return real_stat(path, *args, **kwargs)

    with tempfile.TemporaryDirectory() as directory:
        root = _deep_tree(directory, files)
        with PinnedTree(root) as tree, patch("app.services.fs_preflight.os.stat", side_effect=counting):
            seen = list(tree.entries(on_error=lambda *_: None))
        assert len(seen) == files + 4, seen
    return calls["n"]


class AncestorFenceTests(unittest.TestCase):
    def test_the_fence_costs_the_same_for_ten_files_as_for_two_hundred(self) -> None:
        few, many = _ancestor_stats(10), _ancestor_stats(200)
        self.assertEqual(few, many)
        # Once before and once after each of the four folders, per ancestor:
        # far from the 4 × 200 a per-entry fence made.
        self.assertLessEqual(many, 2 * (1 + 2 + 3 + 4))

    def test_walk_yields_the_listing_stat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _deep_tree(directory, 2)
            with PinnedTree(root) as tree:
                items = list(tree.entries(on_error=lambda *_: None, with_stat=True))
            self.assertEqual(len(items), 6)
            for path, _pinned, info in items:
                actual = path.lstat()
                self.assertEqual((info.st_ino, info.st_mode), (actual.st_ino, actual.st_mode))

    def test_on_listing_names_each_folder_before_its_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _deep_tree(directory, 3)
            order: list[tuple[str, object]] = []
            with PinnedTree(root) as tree:
                for path, _ in tree.entries(
                    on_error=lambda *_: None,
                    on_listing=lambda folder, names: order.append(("listing", folder, sorted(names))),
                ):
                    order.append(("entry", path))
        leaf = root / "library/user/2026/2026-11-15"
        listing = order.index(("listing", leaf, ["img000.jpg", "img001.jpg", "img002.jpg"]))
        self.assertLess(listing, order.index(("entry", leaf / "img000.jpg")))


class InspectEntryProbeTests(unittest.TestCase):
    def test_without_probe_a_regular_file_is_never_opened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.bin"
            target.write_bytes(b"x")
            info = target.lstat()
            with (patch("app.services.fs_preflight.os.open", side_effect=AssertionError("opened")),
                  patch.object(Path, "lstat", side_effect=AssertionError("stat again"))):
                self.assertEqual(inspect_entry(target, info=info, probe=False), ("regular", info))

    def test_an_open_only_probe_never_reads_but_still_refuses_a_denied_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.bin"
            target.write_bytes(b"x")
            info = target.lstat()
            with patch("app.services.fs_preflight.os.read", side_effect=AssertionError("read")):
                self.assertEqual(inspect_entry(target, info=info, read=False), ("regular", info))
            with (patch("app.services.fs_preflight.os.open", side_effect=PermissionError("denied")),
                  self.assertRaises(PermissionError)):
                inspect_entry(target, info=info, read=False)

    def test_the_probe_checks_the_given_stat_against_the_opened_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.bin"
            target.write_bytes(b"x")
            other = Path(directory) / "other.bin"
            other.write_bytes(b"y")
            with self.assertRaises(OSError):
                inspect_entry(target, info=other.lstat())


class TraversalScanTests(_AdoptionFixture):
    def setUp(self) -> None:
        super().setUp()
        for index in range(12):
            (self.albums / f"img{index:02d}.heic").write_bytes(b"x" * (100 + index))

    def _vault(self) -> dict:
        from app.storage import runtime_status
        created = self._adopt()
        self.addCleanup(runtime_status.pop, int(created["id"]), None)
        with self.database.connect() as connection:
            return dict(connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (created["id"],),
            ).fetchone())

    def _scan(self, vault: dict, *extra):
        from app.storage import scan_vault
        with ExitStack() as stack:
            for item in (*_quiet_scan_patches(), *extra):
                stack.enter_context(item)
            return scan_vault(dict(vault))

    def _probes(self):
        """Record, per file name, the walk's (probe, read) arguments."""
        import app.storage as storage
        real_inspect = storage.inspect_entry
        probes: dict[str, list[tuple[bool, bool]]] = {}

        def recording(path, **kwargs):
            result = real_inspect(path, **kwargs)
            if result and result[0] == "regular":
                probes.setdefault(path.name, []).append(
                    (kwargs.get("probe", True), kwargs.get("read", True)),
                )
            return result

        return probes, patch("app.storage.inspect_entry", side_effect=recording)

    def _deny_open(self, name: str):
        """An unreadable file: every open of it fails; record the walk's probes."""
        real_open = os.open
        probe_opens: list[str] = []

        def denying(path, flags, *args, **kwargs):
            if Path(str(path)).name == name:
                if str(path).startswith("/proc/self/fd/"):
                    probe_opens.append(str(path))
                raise PermissionError("denied")
            return real_open(path, flags, *args, **kwargs)

        return probe_opens, patch("app.services.fs_preflight.os.open", side_effect=denying)

    def _digests(self, vault_id: int) -> dict[str, str | None]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT path, local_sha256 FROM files WHERE vault_id=%s", (vault_id,),
            ).fetchall()
        return {row["path"]: row["local_sha256"] for row in rows}

    def _problems(self) -> list[dict]:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT event, message_params FROM notifications WHERE problem_status='open'"
            ).fetchall()

    def test_a_first_adoption_skips_the_probe_and_a_rescan_keeps_it(self) -> None:
        vault = self._vault()
        probes, recording = self._probes()
        result = self._scan(vault, recording)
        self.assertEqual(result["local"], 13)
        self.assertEqual(len(probes), 13)
        self.assertTrue(all(calls == [(False, True)] for calls in probes.values()), probes)

        # Fingerprinted and unchanged: opened, so a chmod is still detected,
        # but never read (#709). A changed file is hashed again, so its probe
        # is skipped.
        (self.albums / "img05.heic").write_bytes(b"changed and longer")
        probes, recording = self._probes()
        self._scan(vault, recording)
        self.assertEqual(probes.pop("img05.heic"), [(False, True)])
        self.assertEqual(len(probes), 12)
        self.assertTrue(all(calls == [(True, False)] for calls in probes.values()), probes)

    def test_a_rescan_never_reads_an_unchanged_fingerprinted_file(self) -> None:
        import app.services.fs_preflight as fs_preflight
        vault = self._vault()
        self._scan(vault)
        with patch.object(fs_preflight.os, "read", side_effect=AssertionError("read")) as read:
            result = self._scan(vault)
        read.assert_not_called()
        self.assertEqual(result["local"], 13)
        self.assertEqual(result.get("local_incomplete", 0), 0)

    def test_an_unreadable_new_file_is_reported_by_the_verification(self) -> None:
        from app.services import watch_plan
        vault = self._vault()
        vault_id = int(vault["id"])
        self.addCleanup(watch_plan.forget, vault_id)
        probe_opens, denied = self._deny_open("img03.heic")
        result = self._scan(vault, denied)
        self.assertEqual(probe_opens, [])  # the walk never opened it
        self.assertEqual(result["local_incomplete"], 1)
        # Not observed by this scan, as when the walk's probe failed: never
        # catalogued, so never admitted for upload.
        self.assertEqual(result["local"], 12)
        self.assertNotIn("img03.heic", self._digests(vault_id))
        self.assertTrue(any(
            row["event"] == "file_unreadable" and "img03.heic" in str(row["message_params"])
            for row in self._problems()
        ))
        # A gap the watcher re-checks, as when the walk found it unreadable.
        self.assertIn("img03.heic", watch_plan.unobserved(vault_id).unreadable)

    def test_a_fingerprinted_file_made_unreadable_is_caught_by_the_probe(self) -> None:
        import app.storage as storage
        vault = self._vault()
        self._scan(vault)
        hashed: list[str] = []
        real_hash = storage.hash_stable_regular_file

        def recording(path):
            hashed.append(path.name)
            return real_hash(path)

        probe_opens, denied = self._deny_open("img03.heic")
        result = self._scan(vault, denied, patch(
            "app.storage.hash_stable_regular_file", side_effect=recording,
        ))
        self.assertEqual(len(probe_opens), 1)
        self.assertNotIn("img03.heic", hashed)
        self.assertEqual(result["local_incomplete"], 1)
        self.assertTrue(any(
            row["event"] == "file_unreadable" and "img03.heic" in str(row["message_params"])
            for row in self._problems()
        ))

    def test_runtime_status_reports_the_traversal(self) -> None:
        import app.storage as storage
        from app.services.runtime_status import snapshot_runtime_status_for_stats
        vault = self._vault()
        vault_id = int(vault["id"])
        real_inspect = storage.inspect_entry
        seen: list[dict] = []

        def observe(path, **kwargs):
            seen.append(snapshot_runtime_status_for_stats(vault_id))
            return real_inspect(path, **kwargs)

        def phases(*extra):
            seen.clear()
            self._scan(vault, patch("app.storage._TRAVERSAL_PROGRESS_SECONDS", 0),
                       patch("app.storage.inspect_entry", side_effect=observe), *extra)
            return list(seen)

        first = phases()
        self.assertEqual({entry["scan_phase"] for entry in first}, {"traversal"})
        # A first adoption has no count to estimate the time left from.
        self.assertEqual({entry.get("traversal_files_expected") for entry in first}, {None})
        # Each file is counted before it is inspected.
        self.assertEqual([entry["traversal_files_seen"] for entry in first], list(range(1, 14)))
        self.assertTrue(all(entry["traversal_files_per_minute"] > 0 for entry in first))
        # No batch was written before the walk's end: no wait, no write yet.
        self.assertEqual({entry["traversal_lock_wait_seconds"] for entry in first}, {0.0})
        self.assertEqual({entry["traversal_write_seconds"] for entry in first}, {0.0})
        self.assertTrue(all(entry["traversal_probe_seconds"] >= 0.0 for entry in first))

        second = phases()
        self.assertEqual({entry["traversal_files_expected"] for entry in second}, {13})

        after = snapshot_runtime_status_for_stats(vault_id)
        self.assertFalse(after["scanning"])
        self.assertIsNone(after.get("traversal_files_seen"))
        self.assertIsNone(after.get("traversal_files_expected"))
        self.assertIsNone(after.get("traversal_probe_seconds"))

    def test_the_wait_for_a_held_vault_row_is_timed_apart_from_the_walk(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        real_inspect = storage.inspect_entry
        holder = self.database.connect()
        self.addCleanup(holder.close)
        held = threading.Event()

        def observe(path, **kwargs):
            # An upload unit takes the Vault row while the walk runs and keeps
            # it for a moment after the walk's last batch is ready to write.
            if not held.is_set():
                holder.execute("SELECT id FROM vaults WHERE id=%s FOR NO KEY UPDATE", (vault_id,))
                held.set()
                timer = threading.Timer(0.5, holder.commit)
                timer.start()
                self.addCleanup(timer.join)
            return real_inspect(path, **kwargs)

        with self.assertLogs("uvicorn.error", "INFO") as logs:
            self._scan(vault, patch("app.storage.inspect_entry", side_effect=observe))
        [record] = [r for r in logs.records if "traversal saw" in r.getMessage()]
        _vault, seen, elapsed, _rate, batches, lock_wait, write, walk, probe = record.args
        self.assertEqual((_vault, seen, batches), (vault_id, 13, 1))
        self.assertGreaterEqual(lock_wait, 0.3)
        self.assertLess(write, lock_wait)
        self.assertAlmostEqual(walk + lock_wait + write, elapsed, delta=0.05)
        # The per-entry file checks are part of the walk (#709).
        self.assertLessEqual(probe, walk + 0.05)


if __name__ == "__main__":
    unittest.main()
