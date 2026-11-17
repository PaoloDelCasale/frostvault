"""Reads per Source Device (#713, ADR-0046).

Seams under test:
1. The Source Device of a path: ``system.LOCATION`` on Unraid shfs, else
   ``st_dev``; cached per folder; a failed lookup never raises.
2. One registry of per-device read slots, shared by scan and uploads,
   with a dynamic limit and cancellable waits.
3. Claim rotation over a window of ready Jobs, and the scan's "hot" files.
4. Scan reads fed round-robin across devices.
5. Server priority: hysteresis on the host iowait from /proc/stat.
6. 4 MiB reads never move multipart part boundaries (#703).
"""
from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.services import source_devices
from app.services.source_devices import (
    DeviceResolver, DeviceSlots, HotFiles, ServerPriority, interleave_by_device,
    parse_cpu_times, rotate_claims,
)
from app.services.shutdown import WorkerStopping


def _stat(dev: int):
    return lambda path, follow_symlinks=False: SimpleNamespace(st_dev=dev)


class DeviceResolverTests(unittest.TestCase):
    def test_unraid_location_names_the_array_disk(self) -> None:
        calls = []

        def getxattr(path, name):
            calls.append((path, name))
            return b"disk2\0"

        resolver = DeviceResolver(getxattr=getxattr, stat=_stat(41))
        self.assertEqual(resolver.device_of("/sources/immich/2024/01/a.jpg"), "location:disk2")
        self.assertEqual(calls, [("/sources/immich/2024/01/a.jpg", "system.LOCATION")])

    def test_cached_per_folder_not_per_file(self) -> None:
        calls = []

        def getxattr(path, name):
            calls.append(path)
            return b"disk1" if "/01/" in path else b"cache"

        resolver = DeviceResolver(getxattr=getxattr, stat=_stat(41))
        for name in ("a.jpg", "b.jpg", "c.mp4"):
            self.assertEqual(resolver.device_of(f"/s/2024/01/{name}"), "location:disk1")
        self.assertEqual(resolver.device_of("/s/2024/02/a.jpg"), "location:cache")
        self.assertEqual(len(calls), 2)

    def test_unsupported_attribute_falls_back_to_st_dev(self) -> None:
        for error in (errno.ENOTSUP, errno.ENODATA, errno.EPERM):
            with self.subTest(error=error):
                def getxattr(path, name, error=error):
                    raise OSError(error, os.strerror(error))

                resolver = DeviceResolver(getxattr=getxattr, stat=_stat(2049))
                self.assertEqual(resolver.device_of("/data/a"), "dev:2049")

    def test_no_xattr_support_at_all_uses_st_dev(self) -> None:
        resolver = DeviceResolver(stat=_stat(7))
        resolver._getxattr = None
        self.assertEqual(resolver.device_of("/data/a"), "dev:7")

    def test_a_failed_lookup_is_unknown_and_not_remembered(self) -> None:
        def missing(path, follow_symlinks=False):
            raise FileNotFoundError(path)

        def getxattr(path, name):
            raise FileNotFoundError(path)

        resolver = DeviceResolver(getxattr=getxattr, stat=missing)
        self.assertEqual(resolver.device_of("/gone/a"), "unknown")
        resolver._stat = _stat(9)
        self.assertEqual(resolver.device_of("/gone/b"), "dev:9")

    def test_real_file_resolves_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.bin"
            path.write_bytes(b"x")
            with patch("builtins.open", side_effect=AssertionError("read")):
                device = DeviceResolver().device_of(path)
        self.assertTrue(device.startswith(("location:", "dev:")))

    def test_cache_is_bounded(self) -> None:
        resolver = DeviceResolver(getxattr=lambda path, name: b"disk1", stat=_stat(1), size=3)
        for index in range(10):
            resolver.device_of(f"/s/{index}/a")
        self.assertEqual(len(resolver._folders), 3)

    def test_vault_paths_join_lexically_without_resolving(self) -> None:
        with patch.object(source_devices, "device_of", side_effect=lambda path: path), \
                patch("pathlib.Path.resolve", side_effect=AssertionError("resolve")), \
                patch("os.path.realpath", side_effect=AssertionError("realpath")):
            self.assertEqual(source_devices.device_of_entry("/sources/a", "2024/x.jpg"),
                             "/sources/a/2024/x.jpg")
            self.assertEqual(source_devices.device_of_entry("/sources/a", "../etc/passwd"), "unknown")

    def test_module_lookup_never_raises(self) -> None:
        with patch.object(source_devices._resolver, "device_of", side_effect=RuntimeError("boom")):
            self.assertEqual(source_devices.device_of("/x/y"), "unknown")


class DeviceSlotTests(unittest.TestCase):
    def test_limit_is_per_device_and_devices_are_independent(self) -> None:
        slots = DeviceSlots(2)
        with slots.reading("a"), slots.reading("a"):
            self.assertEqual(slots.load("a"), 2)
            entered = threading.Event()

            def third():
                with slots.reading("a"):
                    entered.set()

            worker = threading.Thread(target=third)
            worker.start()
            # Another device is free meanwhile.
            with slots.reading("b"):
                self.assertFalse(entered.wait(0.3))
            self.assertEqual(slots.load("a"), 3)  # two reading, one waiting
        worker.join(2)
        self.assertTrue(entered.is_set())
        self.assertEqual((slots.load("a"), slots.load("b")), (0, 0))

    def test_one_registry_is_shared_by_scan_and_upload_reads(self) -> None:
        source_devices.reset_for_tests(limit=1)
        self.addCleanup(source_devices.reset_for_tests)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.bin"
            path.write_bytes(b"x")
            device = source_devices.device_of(path)
            # An upload holds the only slot: a scan read of the same device waits.
            with source_devices.reading(path):
                stopped = threading.Event()

                def check():
                    if stopped.is_set():
                        raise WorkerStopping("cancelled")

                errors = []

                def scan_read():
                    try:
                        with source_devices.slots().reading(device, check):
                            errors.append("entered")
                    except WorkerStopping as exc:
                        errors.append(exc)

                worker = threading.Thread(target=scan_read)
                worker.start()
                time.sleep(0.2)
                self.assertEqual(source_devices.slots().load(device), 2)
                stopped.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], WorkerStopping)
            self.assertEqual(source_devices.slots().load(device), 0)

    def test_checkpoint_runs_while_waiting_and_cancellation_wins(self) -> None:
        slots = DeviceSlots(1)
        calls = []

        def check():
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError("claim lost")

        with slots.reading("a"):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "claim lost"):
                with slots.reading("a", check):
                    self.fail("entered without a slot")
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(slots.load("a"), 0)

    def test_limit_is_read_at_each_acquisition(self) -> None:
        limit = {"value": 3}
        slots = DeviceSlots(lambda: limit["value"])
        with slots.reading("a"):
            limit["value"] = 1  # Throttled: no second read of that device.
            entered = threading.Event()
            worker = threading.Thread(target=lambda: slots.reading("a").__enter__() and None or entered.set())
            worker.daemon = True
            worker.start()
            self.assertFalse(entered.wait(0.3))
            limit["value"] = 3
            slots.wake()
            self.assertTrue(entered.wait(2))

    def test_snapshot_reports_reading_and_waiting(self) -> None:
        slots = DeviceSlots(1)
        with slots.reading("location:disk1"):
            self.assertEqual(slots.snapshot(), [{"device": "disk1", "reading": 1, "waiting": 0,
                                                 "limit": 1, "bytes_per_second": None}])
        self.assertEqual(slots.snapshot(), [])

    def test_configured_limit_comes_from_settings(self) -> None:
        # Unset or empty: chosen per device (#766); set: clamped to 1-16.
        from app.config import source_device_read_concurrency_from_env as parse
        self.assertIsNone(parse({}))
        self.assertIsNone(parse({"SOURCE_DEVICE_READ_CONCURRENCY": " "}))
        self.assertEqual(parse({"SOURCE_DEVICE_READ_CONCURRENCY": "2"}), 2)
        self.assertEqual(parse({"SOURCE_DEVICE_READ_CONCURRENCY": "99"}), 16)
        with patch.dict(os.environ, {"SOURCE_DEVICE_READ_CONCURRENCY": ""}):
            self.assertIsNone(Settings().source_device_read_concurrency)
        self.assertEqual(Settings().server_priority_iowait_percent, 0)
        with patch("app.config.settings", replace(Settings(), source_device_read_concurrency=5)):
            self.assertEqual(source_devices.configured_limit(), 5)


def _upload(job_id: int, device: str, file_id: str | None = None) -> dict:
    return {"id": job_id, "action": "upload", "device": device,
            "file_id": file_id or f"f{job_id}"}


class ClaimRotationTests(unittest.TestCase):
    def rotate(self, jobs, slots=None, hot=None, window=64):
        return [job["id"] for job in rotate_claims(
            jobs, device_of=lambda job: job.get("device"), slots=slots or DeviceSlots(2),
            hot=hot or HotFiles(), window=window,
        )]

    def test_uploads_rotate_to_a_device_with_a_free_slot(self) -> None:
        jobs = [_upload(1, "d1"), _upload(2, "d1"), _upload(3, "d1"),
                _upload(4, "d2"), _upload(5, "d1"), _upload(6, "d2")]
        # Two per device, then the current order.
        self.assertEqual(self.rotate(jobs), [1, 2, 4, 6, 3, 5])

    def test_reads_in_flight_count_against_the_device(self) -> None:
        slots = DeviceSlots(2)
        jobs = [_upload(1, "d1"), _upload(2, "d2")]
        with slots.reading("d1"), slots.reading("d1"):
            self.assertEqual(self.rotate(jobs, slots=slots), [2, 1])

    def test_window_bounds_the_search_then_current_order(self) -> None:
        jobs = [_upload(index, "d1") for index in range(1, 5)] + [_upload(9, "d2")]
        # At the second pick 9 is outside the window: the head goes; then it is in.
        self.assertEqual(self.rotate(jobs, slots=DeviceSlots(1), window=3), [1, 2, 9, 3, 4])

    def test_unknown_device_is_free_and_lookup_errors_never_fail(self) -> None:
        def broken(job):
            raise OSError("xattr")

        jobs = [_upload(1, "x"), _upload(2, "x")]
        result = [job["id"] for job in rotate_claims(
            jobs, device_of=broken, slots=DeviceSlots(1), hot=HotFiles())]
        self.assertEqual(result, [1, 2])

    def test_other_jobs_are_never_overtaken(self) -> None:
        slots = DeviceSlots(1)
        jobs = [_upload(1, "d1"), _upload(2, "d1"),
                {"id": 3, "action": "recover"}, _upload(4, "d2")]
        hot = HotFiles()
        hot.add("f4")
        with slots.reading("d1"):
            # 1 and 2 wait for d1, so the recovery may go first; the hot upload
            # behind the recovery never moves ahead of it.
            self.assertEqual(self.rotate(jobs, slots=slots, hot=hot), [3, 4, 1, 2])
        # With d1 free, 1 goes; 2 then waits for d1 behind the other Jobs.
        self.assertEqual(self.rotate(jobs, slots=DeviceSlots(1), hot=hot), [1, 3, 4, 2])

    def test_a_file_the_scan_just_read_goes_first_once(self) -> None:
        hot = HotFiles()
        hot.add("f5")
        jobs = [_upload(index, "d1") for index in range(1, 6)]
        self.assertEqual(self.rotate(jobs, hot=hot), [5, 1, 2, 3, 4])
        self.assertNotIn("f5", hot)

    def test_hot_files_expire_and_are_bounded(self) -> None:
        now = {"t": 0.0}
        hot = HotFiles(ttl=10, limit=2, clock=lambda: now["t"])
        for file_id in ("a", "b", "c"):
            hot.add(file_id)
        self.assertEqual(hot.ids(), ["b", "c"])
        now["t"] = 11
        self.assertEqual(hot.ids(), [])


class InterleaveTests(unittest.TestCase):
    def test_round_robin_across_devices(self) -> None:
        items = ["1a", "1b", "1c", "2a", "2b", "3a"]
        feed = interleave_by_device(items, device_of=lambda item: item[0],
                                    busy=lambda device: 0, limit=lambda: 3)
        self.assertEqual([step[1] for step in feed], ["1a", "2a", "3a", "1b", "2b", "1c"])

    def test_waits_when_every_buffered_device_is_busy(self) -> None:
        busy = {"1": 1, "2": 1}
        feed = interleave_by_device(["1a", "2a"], device_of=lambda item: item[0],
                                    busy=lambda device: busy[device], limit=lambda: 1)
        self.assertIsNone(next(feed))
        busy["2"] = 0
        self.assertEqual(next(feed), ("2", "2a"))
        busy["1"] = 0
        self.assertEqual(next(feed), ("1", "1a"))
        self.assertIsNone(next(feed, None))

    def test_first_item_needs_only_one_fill(self) -> None:
        seen = []

        def device(item):
            seen.append(item)
            return "d"

        feed = interleave_by_device(range(10_000), device_of=device, busy=lambda d: 0,
                                    limit=lambda: 1, fill=8)
        self.assertEqual(next(feed), ("d", 0))
        self.assertEqual(len(seen), 8)


class ScanReadsPerDeviceTests(unittest.TestCase):
    """The scan's hash pool respects the shared per-device limit."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.rows = []
        for index in range(12):
            name = f"{'a' if index % 3 else 'b'}{index:02d}.bin"
            (self.root / name).write_bytes(b"x" * (10 + index))
            stat_result = (self.root / name).stat()
            self.rows.append({"vault_file_id": f"00000000-0000-0000-0000-{index:012d}", "path": name,
                              "generation": 1, "size": stat_result.st_size,
                              "mtime_ns": stat_result.st_mtime_ns})
        source_devices.reset_for_tests(limit=1)
        self.addCleanup(source_devices.reset_for_tests)

    def _iterate(self, hashing, concurrency=8):
        import app.storage as storage
        return storage._iter_local_fingerprints(
            {"id": 1}, self.root, self.rows,
        ), patch.multiple(
            "app.storage",
            _runtime_settings=lambda: replace(Settings(), scan_hash_concurrency=concurrency),
            hash_stable_regular_file=hashing,
        ), patch.object(source_devices, "device_of",
                        side_effect=lambda path: Path(path).name[0])

    def test_one_read_per_device_and_hot_files(self) -> None:
        import app.storage as storage
        real = storage.hash_stable_regular_file
        lock = threading.Lock()
        active: dict[str, int] = {}
        peak: dict[str, int] = {}

        def hashing(path, **kwargs):
            device = path.name[0]
            with lock:
                active[device] = active.get(device, 0) + 1
                peak[device] = max(peak.get(device, 0), active[device])
            try:
                time.sleep(0.02)
                return real(path, **kwargs)
            finally:
                with lock:
                    active[device] -= 1

        iterator, settings_patch, device_patch = self._iterate(hashing)
        with settings_patch, device_patch:
            updates = list(iterator)
        self.assertEqual(len(updates), 12)
        self.assertEqual(peak, {"a": 1, "b": 1})
        self.assertEqual(set(source_devices.hot_files().ids()),
                         {row["vault_file_id"] for row in self.rows})

    def test_closing_the_scan_stops_reads_waiting_for_a_device(self) -> None:
        import app.storage as storage
        real = storage.hash_stable_regular_file
        read = []

        def hashing(path, **kwargs):
            read.append(path.name)
            return real(path, **kwargs)

        iterator, settings_patch, device_patch = self._iterate(hashing)
        with settings_patch, device_patch:
            # An upload holds device "a": only "b" files can be read.
            with source_devices.slots().reading("a"):
                first = next(iterator)
                self.assertTrue(first["path"].startswith("b"))
                started = time.monotonic()
                iterator.close()
                self.assertLess(time.monotonic() - started, 3)
        self.assertFalse([name for name in read if name.startswith("a")])
        self.assertEqual(source_devices.slots().load("a"), 0)


class ServerPriorityTests(unittest.TestCase):
    def make(self, threshold=40):
        self.now = 0.0
        self.iowait = self.total = 0
        self.changes = []

        def read_stat():
            return (f"cpu  100 0 100 {self.total - self.iowait} {self.iowait} 0 0 0 0 0\n"
                    "cpu0 1 2 3 4 5 6 7 8 9 10\n")

        return ServerPriority(threshold, read_stat=read_stat, clock=lambda: self.now,
                              on_change=self.changes.append)

    def advance(self, priority, seconds, percent, step=2.0):
        elapsed = 0.0
        while elapsed < seconds:
            self.now += step
            elapsed += step
            spent = 1000
            self.total += spent
            self.iowait += int(spent * percent / 100)
            priority.sample()

    def test_parse_aggregate_cpu_line(self) -> None:
        text = "cpu  10 20 30 400 50 6 7 8 9 10\ncpu0 1 1 1 1 1 1 1 1\nintr 1\n"
        self.assertEqual(parse_cpu_times(text), (50, 10 + 20 + 30 + 400 + 50 + 6 + 7 + 8))
        self.assertIsNone(parse_cpu_times("intr 1\n"))

    def test_disabled_by_default_never_reads(self) -> None:
        priority = ServerPriority(0, read_stat=lambda: self.fail("read"))
        self.assertFalse(priority.sample())
        self.assertEqual(priority.status(), {"enabled": False, "throttled": False, "since": None})

    def test_throttles_after_ten_seconds_above_and_recovers_after_thirty_below(self) -> None:
        priority = self.make()
        priority.sample()
        self.advance(priority, 8, 80)
        self.assertFalse(priority.throttled)
        self.advance(priority, 2, 80)
        self.assertTrue(priority.throttled)
        self.assertIsNotNone(priority.status()["since"])
        self.advance(priority, 28, 5)
        self.assertTrue(priority.throttled)
        self.advance(priority, 2, 5)
        self.assertFalse(priority.throttled)
        self.assertEqual(self.changes, [True, False])

    def test_a_short_dip_or_spike_resets_the_clock(self) -> None:
        priority = self.make()
        priority.sample()
        self.advance(priority, 8, 80)
        self.advance(priority, 2, 10)  # one quiet sample
        self.advance(priority, 8, 80)
        self.assertFalse(priority.throttled)
        self.advance(priority, 2, 80)
        self.assertTrue(priority.throttled)
        self.advance(priority, 20, 5)
        self.advance(priority, 2, 90)  # one busy sample while recovering
        self.advance(priority, 28, 5)
        self.assertTrue(priority.throttled)
        self.advance(priority, 2, 5)
        self.assertFalse(priority.throttled)

    def test_throttle_makes_one_read_per_device_and_holds_new_uploads(self) -> None:
        source_devices.reset_for_tests(limit=3, threshold=40)
        self.addCleanup(source_devices.reset_for_tests)
        self.assertEqual(source_devices.slots().limit(), 3)
        source_devices._priority.throttled = True
        self.assertTrue(source_devices.throttled())
        self.assertEqual(source_devices.slots().limit(), 1)
        status = source_devices.status()
        self.assertTrue(status["server_priority_throttled"])
        self.assertEqual(status["source_device_read_limit"], 1)

    def test_unreadable_proc_stat_keeps_the_state(self) -> None:
        def broken():
            raise OSError("no /proc")

        priority = ServerPriority(40, read_stat=broken)
        self.assertFalse(priority.sample())


class ReadSizeTests(unittest.TestCase):
    def test_reads_are_four_mib_and_part_sizes_are_unchanged(self) -> None:
        from app.services import local_files, upload_pipeline, upload_snapshot
        mib = 1024 * 1024
        self.assertEqual(upload_snapshot.CHUNK_SIZE, 4 * mib)
        self.assertEqual(local_files._CHUNK_SIZE, 4 * mib)
        self.assertEqual(upload_snapshot.MULTIPART_THRESHOLD, 8 * mib)
        self.assertEqual(upload_snapshot.DEFAULT_PART_SIZE, 8 * mib)
        self.assertEqual(upload_snapshot.MAX_PART_SIZE, 1024 * mib)
        # The payload budget keeps its accounting (ADR-0034).
        self.assertEqual(upload_pipeline._Limits().network_bytes, 64 * mib)
        self.assertEqual(upload_pipeline.PREPARE_READER_BYTES, 2 * mib)

    def test_scan_part_digests_match_the_upload_snapshot_parts(self) -> None:
        from app.services.local_files import hash_stable_regular_file
        from app.services.upload_snapshot import (
            DEFAULT_PART_SIZE, PartDigests, prepare_upload_snapshot, sha256_b64,
        )
        # Parts end inside a 4 MiB read: boundaries come from the part size.
        size = 2 * DEFAULT_PART_SIZE + 3 * 1024 * 1024 + 17
        data = bytes(range(256)) * (size // 256) + bytes(size % 256)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.bin"
            path.write_bytes(data)
            parts = PartDigests()
            digest, observed = hash_stable_regular_file(path, parts=parts)
            snapshot, _ = prepare_upload_snapshot(path, lambda: None)
        part_size, digests = parts.record(observed.st_size)
        self.assertEqual(part_size, DEFAULT_PART_SIZE)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        expected = [sha256_b64(data[offset:offset + part_size]) for offset in range(0, size, part_size)]
        self.assertEqual([sha256_b64_raw(value) for value in digests], expected)
        self.assertEqual(list(snapshot.part_checksums), expected)
        self.assertEqual(snapshot.plaintext_sha256, digest)


def sha256_b64_raw(value: bytes) -> str:
    import base64
    return base64.b64encode(value).decode("ascii")


if __name__ == "__main__":
    unittest.main()
