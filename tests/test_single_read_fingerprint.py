"""#766: one read per Local Copy, read limits per disk type, rates per device.

Seams under test (none needs a database):
1. An upload's whole, stable Upload Snapshot read of a Local Copy without a
   fingerprint publishes it (scan compare-and-swap): a multipart file before
   its send, a single PUT with its publication or, failing, as the run ends;
   a failed send keeps it; a changed file, a recorded fingerprint or a snapshot
   taken from the catalog publish nothing.
2. The scan verification skips the very observation an upload fingerprinted.
3. The default reads per device: 1 rotational, 3 solid-state or unknown, the
   setting for every device when set.
4. The read rate of each device in the runtime status of ``GET /api/stats``.
"""
from __future__ import annotations

import hashlib
import os
from itertools import islice
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.fixtures import override_settings
from app.services import source_devices, throughput
from app.services import upload_provenance as provenance
from app.services.source_devices import DeviceSlots, RotationalProbe, UploadFingerprints
from app.services.upload_snapshot import MULTIPART_THRESHOLD, prepare_upload_snapshot, upload_part_size

CHANGED = "Local file changed since fingerprinting"
MTIME_NS = 1_700_000_000_123_456_789
BIG = bytes(range(256)) * (MULTIPART_THRESHOLD // 256) + b"tail"
SMALL = b"a small Local Copy"


def _parts(payload: bytes) -> list[bytes]:
    size = upload_part_size(len(payload))
    return [hashlib.sha256(payload[start:start + size]).digest()
            for start in range(0, len(payload), size)]


class UploadFingerprintTests(unittest.TestCase):
    """The upload execution with stubbed catalog units and transport."""

    def setUp(self) -> None:
        from app import storage
        from app.services import upload_execution as execution
        from app.services.upload_pipeline import _Pipeline

        source_devices.reset_for_tests(limit=3)
        self.addCleanup(source_devices.reset_for_tests)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "movie.mkv"
        self.write(BIG)
        self.execution_module = execution
        self.pipeline = _Pipeline(4)
        self.sent: list[dict] = []
        self.events: list[str] = []
        self.bound = False
        self.fingerprint = self.unfingerprinted()
        self.check = lambda: None
        self.catalog = MagicMock()
        self.catalog.publish_local_fingerprint.side_effect = self.published
        worker = MagicMock()
        worker.OperationCancelled, worker.JobLeaseLost = storage.OperationCancelled, storage.JobLeaseLost
        worker.safe_local_path.return_value = self.source
        worker.prepare_upload_snapshot.side_effect = self.hash
        worker._upload_catalog_unit.side_effect = (
            lambda job, mutation, shared=False: mutation(self.catalog))
        worker.upload_s3_file.side_effect = self.transport
        self.worker = worker
        self.execution = execution.UploadExecution(worker)
        attempt = SimpleNamespace(id="attempt", token="token")
        for name, value in {"provider_identity": "s3-compatible-test", "prepare": attempt,
                            "mark_send_started": attempt, "validated_vault": {"id": 2},
                            "ReceiptJournal": MagicMock()}.items():
            self.enterContext(patch.object(provenance, name, return_value=value))
        stages = execution.UploadExecution
        for name in ("_load", "_observe", "_unchanged", "_materialize", "_publish", "_complete"):
            self.enterContext(patch.object(stages, name, getattr(self, name.lstrip("_"))))

    def write(self, payload: bytes) -> None:
        self.source.write_bytes(payload)
        os.utime(self.source, ns=(MTIME_NS, MTIME_NS))

    def unfingerprinted(self, payload: bytes = BIG) -> dict:
        return {"generation": 7, "local_size": len(payload), "local_mtime_ns": MTIME_NS,
                "local_sha256": None, "local_part_size": None, "local_part_sha256": None,
                "fingerprint_is_current": False}

    # -- stubs ---------------------------------------------------------------

    def published(self, *args, **kwargs) -> bool:
        self.events.append("fingerprint")
        return True

    def hash(self, source, check_active, **kwargs):
        def check() -> None:
            check_active()
            self.check()
        return prepare_upload_snapshot(source, check, **kwargs)

    def transport(self, transfer_job, *, source, key):
        self.events.append("send")
        self.sent.append(transfer_job)
        return {"VersionId": "landed"}

    def load(self, run):
        run.observed = self.execution_module._regular_stat(run.source)
        run.extra.update(pin={"bucket": "bucket"}, key_never_written=True,
                         cleanup_pending=False, bound=self.bound)

    def observe(self, run):
        run.generation = 7
        run.extra["fingerprint"] = self.fingerprint
        return True

    def unchanged(self, run):
        return False

    def materialize(self, run, attempt, response):
        run.version = self.execution_module._Version(
            "version", "key", response["VersionId"], run.snapshot.size, "verified", None, "token")

    def publish(self, run):
        pass

    def complete(self, run):
        pass

    def run_job(self):
        job = {"id": 1, "vault_id": 2, "vault_uuid": "vault", "s3_key_layout": "vault_prefix",
               "path": "movie.mkv", "file_id": "file", "source_root": "/sources", "origin": "manual"}
        return self.execution._run_claimed(job, self.pipeline, lambda: None)

    def scan_row(self, payload: bytes = BIG, **changes) -> dict:
        row = {"vault_file_id": "file", "path": "movie.mkv", "generation": 7,
               "size": len(payload), "mtime_ns": MTIME_NS}
        row.update(changes)
        return row

    # -- behavior ------------------------------------------------------------

    def test_a_whole_stable_read_publishes_the_fingerprint_before_the_send(self) -> None:
        self.assertEqual(self.run_job().status, "completed")
        self.catalog.publish_local_fingerprint.assert_called_once_with(
            "file", generation=7, plaintext_sha256=hashlib.sha256(BIG).hexdigest(),
            size=len(BIG), mtime_ns=MTIME_NS, parts=(upload_part_size(len(BIG)), _parts(BIG)))
        self.assertEqual(self.events, ["fingerprint", "send"])
        # The multipart send checks its parts against exactly these digests.
        snapshot = self.sent[0]["_upload_snapshot"]
        self.assertEqual(snapshot.local_parts(), (upload_part_size(len(BIG)), _parts(BIG)))
        fingerprinted = source_devices.upload_fingerprints()
        for other in (dict(generation=8), dict(size=len(BIG) - 1), dict(mtime_ns=MTIME_NS + 1),
                      dict(vault_file_id="other")):
            with self.subTest(other=other):
                self.assertFalse(fingerprinted.claim(self.scan_row(**other)))
        self.assertTrue(fingerprinted.claim(self.scan_row()))
        self.assertFalse(fingerprinted.claim(self.scan_row()), "one skip per publication")

    def test_a_single_put_sends_exactly_the_fingerprinted_bytes(self) -> None:
        self.write(SMALL)
        self.fingerprint = self.unfingerprinted(SMALL)
        self.assertEqual(self.run_job().status, "completed")
        self.catalog.publish_local_fingerprint.assert_called_once_with(
            "file", generation=7, plaintext_sha256=hashlib.sha256(SMALL).hexdigest(),
            size=len(SMALL), mtime_ns=MTIME_NS, parts=None)
        self.assertEqual(self.sent[0]["_upload_payload"], SMALL)

    def test_the_fingerprint_stays_when_the_send_fails_after_the_whole_read(self) -> None:
        self.worker.upload_s3_file.side_effect = RuntimeError("provider down")
        with self.assertRaisesRegex(RuntimeError, "provider down"):
            self.run_job()
        self.catalog.publish_local_fingerprint.assert_called_once()
        self.assertTrue(source_devices.upload_fingerprints().claim(self.scan_row()))

    def test_a_small_failed_send_keeps_its_fingerprint_too(self) -> None:
        self.write(SMALL)
        self.fingerprint = self.unfingerprinted(SMALL)
        self.worker.upload_s3_file.side_effect = RuntimeError("provider down")
        with self.assertRaisesRegex(RuntimeError, "provider down"):
            self.run_job()
        self.catalog.publish_local_fingerprint.assert_called_once()
        self.worker.upload_s3_file.assert_called_once()  # Published after the failed send.
        self.assertTrue(source_devices.upload_fingerprints().claim(self.scan_row(SMALL)))

    def test_a_small_successful_upload_adds_no_unit(self) -> None:
        # The publication unit records the same digest of the same observation.
        def publish(_execution, run):
            run.extra["read_fingerprint_published"] = True
            self.execution._read_fingerprint_committed(run)

        self.write(SMALL)
        self.fingerprint = self.unfingerprinted(SMALL)
        with patch.object(self.execution_module.UploadExecution, "_publish", publish):
            self.assertEqual(self.run_job().status, "completed")
        self.catalog.publish_local_fingerprint.assert_not_called()
        self.assertTrue(source_devices.upload_fingerprints().claim(self.scan_row(SMALL)))

    def test_a_file_changed_during_the_read_publishes_nothing(self) -> None:
        calls = {"n": 0}

        def touch() -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # Inside the read loop.
                os.utime(self.source, ns=(MTIME_NS + 1, MTIME_NS + 1))

        self.check = touch
        with self.assertRaisesRegex(RuntimeError, CHANGED):
            self.run_job()
        self.catalog.publish_local_fingerprint.assert_not_called()
        self.worker.upload_s3_file.assert_not_called()
        self.assertFalse(source_devices.upload_fingerprints().claim(self.scan_row()))

    def test_a_size_changed_during_the_read_publishes_nothing(self) -> None:
        calls = {"n": 0}

        def grow() -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                with self.source.open("ab") as handle:
                    handle.write(b"more")
                os.utime(self.source, ns=(MTIME_NS, MTIME_NS))

        self.check = grow
        with self.assertRaisesRegex(RuntimeError, CHANGED):
            self.run_job()
        self.catalog.publish_local_fingerprint.assert_not_called()

    def test_a_lost_compare_and_swap_is_not_remembered(self) -> None:
        self.catalog.publish_local_fingerprint.side_effect = lambda *a, **k: False
        self.assertEqual(self.run_job().status, "completed")
        self.assertFalse(source_devices.upload_fingerprints().claim(self.scan_row()))

    def test_no_new_fingerprint_without_a_whole_read_or_over_a_recorded_one(self) -> None:
        recorded = {**self.unfingerprinted(), "local_sha256": hashlib.sha256(BIG).digest(),
                    "local_part_size": upload_part_size(len(BIG)), "local_part_sha256": _parts(BIG)}
        cases = {
            # #703: the catalog's snapshot is used, nothing is read.
            "snapshot from the catalog": dict(fingerprint=recorded),
            # Bound to its snapshot, the file is read but already fingerprinted.
            "bound, already fingerprinted": dict(fingerprint=recorded, bound=True),
            "another generation": dict(fingerprint={**self.unfingerprinted(), "generation": 6}),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                self.catalog.publish_local_fingerprint.reset_mock()
                self.fingerprint, self.bound = case["fingerprint"], case.get("bound", False)
                self.assertEqual(self.run_job().status, "completed")
                self.catalog.publish_local_fingerprint.assert_not_called()

    def test_a_retired_fingerprint_is_no_longer_skipped_by_the_scan(self) -> None:
        # A bound retry rehashes and finds other bytes under the same size and
        # mtime: the recorded fingerprint is retired (#703), the skip dropped.
        source_devices.upload_fingerprints().add("file", generation=7, size=len(BIG),
                                                mtime_ns=MTIME_NS)
        self.fingerprint = {**self.unfingerprinted(), "local_sha256": hashlib.sha256(b"x").digest(),
                            "local_part_size": upload_part_size(len(BIG)),
                            "local_part_sha256": _parts(BIG)}
        self.bound = True
        self.assertEqual(self.run_job().status, "completed")
        self.catalog.retire_local_fingerprint.assert_called_once()
        self.assertFalse(source_devices.upload_fingerprints().claim(self.scan_row()))

    def test_the_scan_verification_skips_what_the_upload_fingerprinted(self) -> None:
        from app import storage

        self.assertEqual(self.run_job().status, "completed")
        with patch.object(storage, "hash_stable_regular_file",
                          side_effect=AssertionError("read again")):
            self.assertEqual(storage._hash_local_copy(self.source.parent, self.scan_row()),
                             ("fingerprinted", None))
        # Another observation of the same file is read as usual.
        kind, update = storage._hash_local_copy(self.source.parent, self.scan_row(generation=8))
        self.assertEqual(kind, "hashed")
        self.assertEqual(update["digest"], hashlib.sha256(BIG).hexdigest())

    def test_the_skip_is_checked_again_after_waiting_for_the_device(self) -> None:
        from app import storage

        row = self.scan_row()
        slots = source_devices.slots()
        original = slots.reading

        def reading(device, check=None):
            # The upload publishes while the scan waits for its slot.
            source_devices.upload_fingerprints().add("file", generation=7, size=len(BIG),
                                                    mtime_ns=MTIME_NS)
            return original(device, check)

        with patch.object(slots, "reading", side_effect=reading), \
                patch.object(storage, "hash_stable_regular_file",
                             side_effect=AssertionError("read again")):
            self.assertEqual(storage._hash_local_copy(self.source.parent, row),
                             ("fingerprinted", None))


class UploadFingerprintRegistryTests(unittest.TestCase):
    def test_bounded_and_expiring(self) -> None:
        now = {"t": 0.0}
        registry = UploadFingerprints(ttl=10, limit=2, clock=lambda: now["t"])
        row = lambda file_id: {"vault_file_id": file_id, "generation": 1, "size": 2, "mtime_ns": 3}  # noqa: E731
        for file_id in ("a", "b", "c"):
            registry.add(file_id, generation=1, size=2, mtime_ns=3)
        self.assertFalse(registry.claim(row("a")), "bounded: the oldest is forgotten")
        self.assertTrue(registry.claim(row("c")))
        registry.add("c", generation=1, size=2, mtime_ns=3)
        now["t"] = 11
        self.assertFalse(registry.claim(row("c")), "expired")
        self.assertFalse(registry.claim({"vault_file_id": "c", "generation": None}))

    def test_a_retired_fingerprint_is_read_again(self) -> None:
        registry = UploadFingerprints()
        registry.add("a", generation=1, size=2, mtime_ns=3)
        registry.add("a", generation=2, size=2, mtime_ns=3)
        registry.add("b", generation=1, size=2, mtime_ns=3)
        registry.discard("a")
        self.assertFalse(registry.claim({"vault_file_id": "a", "generation": 2, "size": 2, "mtime_ns": 3}))
        self.assertTrue(registry.claim({"vault_file_id": "b", "generation": 1, "size": 2, "mtime_ns": 3}))


class _Sysfs:
    """A fake ``/sys`` with block devices and their ``queue/rotational``."""

    def __init__(self, root: Path):
        self.root = root

    def disk(self, name: str, rotational: str | None, *, number: str | None = None,
             partitions: dict[str, str] | None = None, slaves: list[str] | None = None) -> None:
        device = self.root / "devices" / name
        (device / "queue").mkdir(parents=True, exist_ok=True)
        if rotational is not None:
            (device / "queue" / "rotational").write_text(rotational + "\n")
        (self.root / "block").mkdir(parents=True, exist_ok=True)
        (self.root / "block" / name).symlink_to(device)
        if number:
            self.link(number, device)
        for part, part_number in (partitions or {}).items():
            (device / part).mkdir()
            (device / part / "partition").write_text("1\n")
            self.link(part_number, device / part)
        if slaves:
            (device / "slaves").mkdir()
            for slave in slaves:
                (device / "slaves" / slave).symlink_to(self.root / "devices" / slave)

    def link(self, number: str, target: Path) -> None:
        (self.root / "dev" / "block").mkdir(parents=True, exist_ok=True)
        (self.root / "dev" / "block" / number).symlink_to(target)


def _dev(major: int, minor: int) -> str:
    return f"dev:{os.makedev(major, minor)}"


class RotationalDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        sysfs = _Sysfs(self.root / "sys")
        sysfs.disk("sda", "1", number="8:0", partitions={"sda1": "8:1"})
        sysfs.disk("nvme0n1", "0", number="259:0", partitions={"nvme0n1p1": "259:1"})
        sysfs.disk("sdc", "1")
        sysfs.disk("sdd", "0")
        sysfs.disk("md1p1", "1")
        sysfs.disk("md2p1", None)  # A release whose md disk has no answer.
        sysfs.disk("dm-0", "0", number="253:0", slaves=["sda", "sdd"])
        sysfs.disk("dm-1", "1", number="253:1", slaves=["nvme0n1", "sdd"])
        sysfs.disk("weird", "maybe", number="7:0")
        self.mdstat = self.root / "mdstat"
        self.mdstat.write_text("sbName=/boot/config/super.dat\ndiskName.2=md2p1\nrdevName.2=sdc\n"
                               "diskName.3=md3p1\nrdevName.3=../etc\n")
        self.probe = RotationalProbe(sys_root=str(self.root / "sys"), mdstat=str(self.mdstat))
        source_devices.reset_for_tests(sys_root=str(self.root / "sys"), mdstat=str(self.mdstat))
        self.addCleanup(source_devices.reset_for_tests)
        self.settings = self.enterContext(override_settings(source_device_read_concurrency=None))

    def test_disk_types(self) -> None:
        cases = {
            _dev(8, 1): True,          # Partition of a rotational disk.
            _dev(8, 0): True,
            _dev(259, 1): False,       # NVMe partition.
            _dev(253, 0): True,        # dm with one rotational member.
            _dev(253, 1): False,       # dm whose members are all solid-state.
            _dev(7, 0): None,          # Unreadable answer.
            _dev(0, 52): None,         # FUSE (shfs) anonymous device.
            "location:disk1": True,    # Unraid md1p1.
            "location:disk2": True,    # md2p1 silent: rdevName.2 of /proc/mdstat.
            "location:disk3": None,    # A member name that is a path is refused.
            "location:disk9": None,
            "location:cache": None,    # A pool: unknown.
            "unknown": None,
            "dev:not-a-number": None,
        }
        for device, expected in cases.items():
            with self.subTest(device=device):
                self.assertIs(self.probe.rotational(device), expected)

    def test_default_limits_per_device(self) -> None:
        self.assertEqual(source_devices.configured_limit(_dev(8, 1)), 1)
        self.assertEqual(source_devices.configured_limit("location:disk1"), 1)
        self.assertEqual(source_devices.configured_limit(_dev(259, 1)), 3)
        self.assertEqual(source_devices.configured_limit("location:cache"), 3)
        self.assertEqual(source_devices.configured_limit("unknown"), 3)
        self.assertEqual(source_devices.configured_limit(), 3)
        slots = source_devices.slots()
        self.assertEqual((slots.limit("location:disk1"), slots.limit("location:cache")), (1, 3))

    def test_the_setting_applies_to_every_device(self) -> None:
        with override_settings(source_device_read_concurrency=2):
            for device in (_dev(8, 1), _dev(259, 1), "location:disk1", "location:cache", "unknown"):
                with self.subTest(device=device):
                    self.assertEqual(source_devices.configured_limit(device), 2)

    def test_slots_bound_each_device_by_its_own_limit(self) -> None:
        slots = DeviceSlots(lambda device: 1 if device == "hdd" else 3)
        with slots.reading("hdd"), slots.reading("ssd"), slots.reading("ssd"):
            self.assertEqual(slots.load("ssd"), 2)
        # The busy HDD is at its limit of 1: the SSD is served, then the
        # feed waits (None) instead of reading the HDD again.
        order = list(islice(source_devices.interleave_by_device(
            ["h1", "h2", "s1", "s2"], device_of=lambda item: "hdd" if item[0] == "h" else "ssd",
            busy=lambda device: 1 if device == "hdd" else 0,
            limit=lambda device: 1 if device == "hdd" else 3), 3))
        self.assertEqual(order, [("ssd", "s1"), ("ssd", "s2"), None])


class DeviceRateTests(unittest.TestCase):
    def setUp(self) -> None:
        source_devices.reset_for_tests(limit=3)
        self.addCleanup(source_devices.reset_for_tests)
        throughput.METER.reset()
        self.addCleanup(throughput.METER.reset)

    def test_reads_under_a_slot_are_counted_per_device(self) -> None:
        slots = source_devices.slots()
        source_devices.note_read(10)  # Outside any slot: not counted.
        with source_devices.reading(Path("/nowhere/file")):
            pass
        with slots.reading("location:disk1"):
            source_devices.note_read(4096)
            with slots.reading("location:disk2"):
                source_devices.note_read(1)
            source_devices.note_read(4)
        self.assertEqual(source_devices.read_totals(), {"location:disk1": 4100, "location:disk2": 1})

    def test_bytes_per_second_from_the_samples(self) -> None:
        meter = throughput.ThroughputMeter()
        meter.record_devices(mono=100.0, totals={"location:disk1": 0})
        self.assertEqual(meter.device_rates(mono=100.0), {"location:disk1": None})
        meter.record_devices(mono=110.0, totals={"location:disk1": 50_000_000, "location:disk2": 7})
        rates = meter.device_rates(mono=110.0)
        self.assertEqual(rates, {"location:disk1": 5_000_000, "location:disk2": None})
        # Stale samples claim no rate.
        self.assertEqual(meter.device_rates(mono=110.0 + throughput.STALE_SECONDS + 1),
                         {"location:disk1": None, "location:disk2": None})

    def test_stats_runtime_reports_the_rate_of_each_device(self) -> None:
        slots = source_devices.slots()
        with slots.reading("location:disk1"):
            source_devices.note_read(1_000)
        now = time.monotonic()
        throughput.METER.record_devices(mono=now - 20, totals={"location:disk1": 0})
        throughput.METER.record_devices(mono=now, totals=source_devices.read_totals())
        status = source_devices.status()
        self.assertEqual(status["source_devices"], [
            {"device": "disk1", "reading": 0, "waiting": 0, "limit": 3, "bytes_per_second": 50}])
        with slots.reading("location:disk2"):
            listed = {entry["device"]: entry for entry in source_devices.status()["source_devices"]}
            self.assertEqual(listed["disk2"]["bytes_per_second"], None)
            self.assertEqual(listed["disk2"]["reading"], 1)

    def test_the_sampler_records_device_totals(self) -> None:
        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args, **kwargs):
                return SimpleNamespace(fetchall=lambda: [])

        with source_devices.slots().reading("location:disk1"):
            source_devices.note_read(500)
        meter = throughput.ThroughputMeter()
        throughput.sample(_Connection, meter=meter, mono=1.0, held_back=False)
        throughput.sample(_Connection, meter=meter, mono=21.0, held_back=False)
        self.assertEqual(meter.device_rates(mono=21.0), {"location:disk1": 0})


if __name__ == "__main__":
    unittest.main()
