"""In-memory speeds and the one expected finish of an adoption (#755); DB-free."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import Mock, patch

from app.services import throughput
from app.services.throughput import (
    AVERAGE_SECONDS, HISTORY_SECONDS, SAMPLE_SECONDS, STOPPED_SECONDS, ThroughputMeter, annotate,
    expected_finish,
)

NOW = datetime(2026, 11, 17, 16, 33, tzinfo=timezone.utc)
MB = 1_000_000


def row(*, settled=0, settled_files=0, recent=0, recent_files=0, held=0):
    return {"settled_bytes": settled, "settled_files": settled_files, "recent_bytes": recent,
            "recent_files": recent_files, "held_bytes": held}


class MeterTests(unittest.TestCase):
    def feed(self, meter, steps, *, start=0.0, vault=3, upload=None, verify=None):
        """Feed ``steps`` samples 2 s apart; ``upload(i)`` and ``verify(i)`` give the row and bytes."""
        for i in range(steps):
            mono = start + i * SAMPLE_SECONDS
            meter.record(mono=mono, wall=NOW.timestamp() + mono, cut=None,
                         uploads={vault: upload(i)} if upload else {},
                         verification={vault: ("scan-1", verify(i))} if verify else {})
        return start + (steps - 1) * SAMPLE_SECONDS

    def test_in_flight_bytes_move_the_instant_speed_before_any_file_completes(self):
        meter = ThroughputMeter()
        # One big file sending 12 MB/s: nothing completes in the window.
        last = self.feed(meter, 30, upload=lambda i: row(held=i * 24 * MB))
        speeds = meter.snapshot(3, mono=last)
        self.assertEqual(speeds["upload"]["bytes_per_second"], 12 * MB)
        self.assertEqual(speeds["upload"]["files_per_second"], 0)

    def test_a_completion_moves_bytes_from_held_to_completed_without_a_jump(self):
        meter = ThroughputMeter()
        # Held bytes grow to 10 MB; at sample 5 the Job completes (recent),
        # at sample 8 it is settled. The raw sum never jumps.
        def upload(i):
            if i < 5:
                return row(held=i * 2 * MB)
            if i < 8:
                return row(recent=10 * MB, recent_files=1)
            return row(settled=10 * MB if i == 8 else 0, settled_files=1 if i == 8 else 0)
        last = self.feed(meter, 12, upload=upload)
        samples = list(meter._vaults[3].samples)
        self.assertEqual([sample[2] for sample in samples],
                         [0, 2 * MB, 4 * MB, 6 * MB, 8 * MB] + [10 * MB] * 7)
        self.assertEqual(samples[-1][3], 1)
        self.assertEqual(meter.snapshot(3, mono=last)["upload"]["files_per_second"], round(1 / 22, 2))

    def test_a_failed_send_never_counts_negative(self):
        meter = ThroughputMeter()
        held = [0, 4 * MB, 8 * MB, 0, 2 * MB]
        self.feed(meter, len(held), upload=lambda i: row(held=held[i]))
        self.assertEqual([sample[2] for sample in meter._vaults[3].samples],
                         [0, 4 * MB, 8 * MB, 8 * MB, 10 * MB])

    def test_average_is_over_five_minutes_and_ignores_an_idle_start(self):
        meter = ThroughputMeter()
        # 10 idle minutes, then 3 minutes at 2 MB per sample (1 MB/s).
        idle = 300
        last = self.feed(meter, idle + 91, upload=lambda i: row(held=max(0, i - idle) * 2 * MB))
        speeds = meter.snapshot(3, mono=last)
        self.assertEqual(speeds["upload"]["average_bytes_per_second"], MB)
        # Later, a slower pace replaces the old one within five minutes.
        meter = ThroughputMeter()
        last = self.feed(meter, 600, upload=lambda i: row(held=(i * 4 if i < 300 else 1200 + (i - 300)) * MB))
        self.assertEqual(meter.snapshot(3, mono=last)["upload"]["average_bytes_per_second"], MB // 2)

    def test_no_average_before_a_minute_of_movement(self):
        meter = ThroughputMeter()
        last = self.feed(meter, 20, upload=lambda i: row(held=i * MB))
        speeds = meter.snapshot(3, mono=last)
        self.assertIsNone(speeds["upload"]["average_bytes_per_second"])
        self.assertIsNotNone(speeds["upload"]["bytes_per_second"])

    def test_verification_bytes_restart_with_each_scan(self):
        meter = ThroughputMeter()
        for i, (scan, done) in enumerate([("a", 0), ("a", 4 * MB), ("a", 8 * MB), (None, 0),
                                          ("b", 2 * MB), ("b", 6 * MB)]):
            meter.record(mono=i * 2.0, wall=0, cut=None, uploads={},
                         verification={3: (scan, done)})
        self.assertEqual([sample[4] for sample in meter._vaults[3].samples],
                         [0, 4 * MB, 8 * MB, 8 * MB, 10 * MB, 14 * MB])

    def test_installation_counts_every_vault_and_history_covers_fifteen_minutes(self):
        meter = ThroughputMeter()
        steps = HISTORY_SECONDS // SAMPLE_SECONDS + 60
        for i in range(steps):
            meter.record(mono=i * 2.0, wall=NOW.timestamp() + i * 2, cut=None,
                         uploads={1: row(held=i * 2 * MB), 2: row(held=i * 4 * MB)},
                         verification={1: ("s", i * 6 * MB)})
        last = (steps - 1) * 2.0
        total = meter.snapshot(None, mono=last)
        self.assertEqual(total["upload"]["bytes_per_second"], 3 * MB)
        self.assertEqual(total["verification"]["average_bytes_per_second"], 3 * MB)
        history = total["history"]
        self.assertEqual(len(history), 30)
        self.assertEqual({point["upload_bytes_per_second"] for point in history}, {3 * MB})
        self.assertLess(history[0]["at"], history[-1]["at"])
        self.assertEqual(len(meter._vaults[1].samples), HISTORY_SECONDS // SAMPLE_SECONDS + 1)

    def test_stale_samples_claim_no_speed(self):
        meter = ThroughputMeter()
        last = self.feed(meter, 60, upload=lambda i: row(held=i * MB))
        speeds = meter.snapshot(3, mono=last + 60)
        self.assertIsNone(speeds["upload"]["bytes_per_second"])
        self.assertEqual(speeds["history"], [])
        self.assertIsNone(meter.snapshot(99, mono=last)["upload"]["bytes_per_second"])


class ExpectedFinishTests(unittest.TestCase):
    def test_the_later_phase_limits_the_finish(self):
        # The issue's adoption: 514.7 GiB to upload at ~11 MB/s, 500 GiB to read at ~27 MB/s.
        finish = expected_finish(now=NOW, upload_left=514 * 2**30, upload_average=11 * MB,
                                 verification_left=500 * 2**30, verification_average=27 * MB)
        self.assertEqual(finish["limited_by"], "upload")
        self.assertEqual(finish["seconds"], round(514 * 2**30 / (11 * MB)))
        self.assertEqual(finish["at"], (NOW + timedelta(seconds=514 * 2**30 / (11 * MB))).isoformat())
        finish = expected_finish(now=NOW, upload_left=MB, upload_average=MB,
                                 verification_left=600 * MB, verification_average=10 * MB)
        self.assertEqual((finish["limited_by"], finish["seconds"]), ("verification", 60))

    def test_only_the_active_phase_counts(self):
        finish = expected_finish(now=NOW, upload_left=0, upload_average=None,
                                 verification_left=60 * MB, verification_average=MB)
        self.assertEqual(finish["limited_by"], "verification")
        self.assertIsNone(expected_finish(now=NOW, upload_left=0, upload_average=MB,
                                          verification_left=0, verification_average=MB))

    def test_an_unmeasured_active_phase_gives_no_finish(self):
        self.assertIsNone(expected_finish(now=NOW, upload_left=MB, upload_average=None,
                                          verification_left=MB, verification_average=MB))
        self.assertIsNone(expected_finish(now=NOW, upload_left=MB, upload_average=0,
                                          verification_left=0, verification_average=None))


def summary(**stages):
    base = {stage: {"files": 0, "bytes": 0, "transferred_bytes": 0}
            for stage in ("stability_wait", "queued", "uploading", "verifying")}
    base.update(stages)
    return base


class AnnotateTests(unittest.TestCase):
    def setUp(self):
        self.meter = ThroughputMeter()
        for i in range(200):
            self.meter.record(mono=i * 2.0, wall=0, cut=None,
                              uploads={1: row(held=i * 2 * MB), 2: row(held=i * 4 * MB)},
                              verification={1: ("s", i * 20 * MB)})
        self.mono = 199 * 2.0
        status = {1: {"scanning": True, "scan_phase": "verification",
                      "verify_bytes_total": 6_000 * MB, "verify_bytes_done": 0}}
        patcher = patch.dict(throughput.runtime_status, status, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def activity(self, *, paused=False):
        queued = {"files": 10, "bytes": 1_200 * MB, "transferred_bytes": 0}
        return {"global": summary(), "vaults": [
            {"vault_id": 1, "paused": False, **summary(queued=queued)},
            {"vault_id": 2, "paused": paused, **summary(
                queued=queued, uploading={"files": 1, "bytes": 1_000 * MB, "transferred_bytes": 200 * MB})},
        ]}

    def test_each_vault_finishes_with_its_slower_phase_and_the_installation_with_its_last_vault(self):
        result = annotate(self.activity(), meter=self.meter, now=NOW, mono=self.mono)
        first, second = result["vaults"]
        # Vault 1: 1,200 MB at 1 MB/s against 6,000 MB read at 10 MB/s.
        self.assertEqual(first["expected_finish"]["limited_by"], "upload")
        self.assertEqual(first["expected_finish"]["seconds"], 1200)
        self.assertEqual(first["throughput"]["verification"]["bytes_per_second"], 10 * MB)
        # Vault 2: 2,000 MB at 2 MB/s, nothing to verify.
        self.assertEqual(second["expected_finish"], {
            "at": (NOW + timedelta(seconds=1000)).isoformat(), "seconds": 1000, "limited_by": "upload"})
        self.assertEqual(result["global"]["expected_finish"], first["expected_finish"])
        self.assertEqual(result["global"]["throughput"]["upload"]["bytes_per_second"], 3 * MB)

    def test_a_paused_vault_has_no_finish_and_does_not_hold_the_installation(self):
        result = annotate(self.activity(paused=True), meter=self.meter, now=NOW, mono=self.mono)
        self.assertIsNone(result["vaults"][1]["expected_finish"])
        self.assertEqual(result["global"]["expected_finish"]["seconds"], 1200)

    def test_an_unmeasured_working_vault_leaves_the_installation_finish_unknown(self):
        activity = self.activity()
        activity["vaults"].append({"vault_id": 7, "paused": False,
                                   **summary(queued={"files": 1, "bytes": MB, "transferred_bytes": 0})})
        result = annotate(activity, meter=self.meter, now=NOW, mono=self.mono)
        self.assertIsNone(result["vaults"][2]["expected_finish"])
        self.assertIsNone(result["global"]["expected_finish"])


class StoppedTests(unittest.TestCase):
    """No progress for five minutes: no finish, stopped since the last progress (#759)."""

    def setUp(self):
        patcher = patch.dict(throughput.runtime_status, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.meter = ThroughputMeter()
        self.wall0 = NOW.timestamp()

    def sample(self, i, *, held, vault=3, scans=None, held_back=False):
        self.meter.record(mono=i * 2.0, wall=self.wall0 + i * 2, cut=None,
                          uploads={vault: row(held=held)}, verification={},
                          scans=scans, held_back=held_back)

    def activity(self, *, queued=10, stability_wait=0):
        vault = {"vault_id": 3, "paused": False, **summary(
            queued={"files": queued, "bytes": queued * MB, "transferred_bytes": 0},
            stability_wait={"files": stability_wait, "bytes": stability_wait * MB, "transferred_bytes": 0})}
        return {"global": summary(), "vaults": [vault]}

    def test_a_vault_with_work_and_no_progress_for_five_minutes_has_no_finish(self):
        # Moving for 3 minutes, then nothing for 5 more.
        for i in range(90):
            self.sample(i, held=i * MB)
        last_progress = self.wall0 + 89 * 2
        for i in range(90, 90 + STOPPED_SECONDS // SAMPLE_SECONDS + 1):
            self.sample(i, held=89 * MB)
        mono = (90 + STOPPED_SECONDS // SAMPLE_SECONDS) * 2.0
        result = annotate(self.activity(), meter=self.meter, now=NOW, mono=mono)
        vault = result["vaults"][0]
        self.assertIsNone(vault["expected_finish"])
        self.assertEqual(vault["stopped_since"], datetime.fromtimestamp(last_progress, timezone.utc).isoformat())
        self.assertEqual(result["global"]["stopped_since"], vault["stopped_since"])
        self.assertIsNone(result["global"]["expected_finish"])
        # Just under five minutes it still is moving: no stop claimed.
        result = annotate(self.activity(), meter=self.meter, now=NOW, mono=89 * 2.0 + STOPPED_SECONDS - 2)
        self.assertIsNone(result["vaults"][0]["stopped_since"])
        # Progress again ends the stop.
        self.sample(mono / 2 + 1, held=90 * MB)
        result = annotate(self.activity(), meter=self.meter, now=NOW, mono=mono + 2)
        self.assertIsNone(result["vaults"][0]["stopped_since"])

    def test_a_moving_scan_or_held_back_reads_are_not_a_stop(self):
        steps = STOPPED_SECONDS // SAMPLE_SECONDS + 10
        for i in range(steps):
            self.sample(i, held=0, scans={3: ("scan", "traversal", i * 100, None, None)})
        mono = (steps - 1) * 2.0
        self.assertIsNone(annotate(self.activity(), meter=self.meter, now=NOW, mono=mono)["vaults"][0]["stopped_since"])
        meter = self.meter = ThroughputMeter()
        for i in range(steps):
            self.sample(i, held=0, held_back=True)
        self.assertIsNone(annotate(self.activity(), meter=meter, now=NOW, mono=mono)["vaults"][0]["stopped_since"])

    def test_files_waiting_to_be_stable_are_not_a_stop(self):
        steps = STOPPED_SECONDS // SAMPLE_SECONDS + 10
        for i in range(steps):
            self.sample(i, held=0)
        mono = (steps - 1) * 2.0
        result = annotate(self.activity(queued=0, stability_wait=4), meter=self.meter, now=NOW, mono=mono)
        self.assertIsNone(result["vaults"][0]["stopped_since"])
        self.assertIsNone(result["global"]["stopped_since"])

    def test_work_that_appears_after_an_idle_vault_starts_its_own_clock(self):
        steps = STOPPED_SECONDS // SAMPLE_SECONDS + 10
        for i in range(steps):
            self.sample(i, held=0)
        mono = (steps - 1) * 2.0
        annotate(self.activity(queued=0), meter=self.meter, now=NOW, mono=mono)
        result = annotate(self.activity(), meter=self.meter, now=NOW, mono=mono)
        self.assertIsNone(result["vaults"][0]["stopped_since"])

    def test_a_long_read_of_one_large_file_is_not_a_stop(self):
        # A 30 GB video at ~14 MB/s: no file completes for over half an hour,
        # only the bytes read by the hashing threads move (#759).
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []

        @contextmanager
        def connect():
            yield connection
        steps = STOPPED_SECONDS // SAMPLE_SECONDS + 30
        status = {"scanning": True, "scan_phase": "verification", "scan_id": "s",
                  "verify_files_total": 1, "verify_files_done": 0,
                  "verify_bytes_total": 30_000 * MB, "verify_bytes_done": 0, "verify_bytes_read": 0}
        with patch.dict(throughput.runtime_status, {3: status}, clear=True):
            for i in range(steps):
                status["verify_bytes_read"] = i * 28 * MB
                throughput.sample(connect, meter=self.meter, now=NOW + timedelta(seconds=2 * i),
                                  mono=i * 2.0, held_back=False)
            mono = (steps - 1) * 2.0
            result = annotate(self.activity(queued=0), meter=self.meter, now=NOW, mono=mono)
            self.assertIsNone(result["vaults"][0]["stopped_since"])
            # The same read with no byte moving is a stop.
            for i in range(steps, 2 * steps):
                throughput.sample(connect, meter=self.meter, now=NOW + timedelta(seconds=2 * i),
                                  mono=i * 2.0, held_back=False)
            result = annotate(self.activity(queued=0), meter=self.meter, now=NOW, mono=(2 * steps - 1) * 2.0)
            self.assertIsNotNone(result["vaults"][0]["stopped_since"])

    def test_a_vault_waiting_in_line_behind_another_is_not_stopped(self):
        # Vault 3 has only queued files; Vault 4 uploads. Claims are in order
        # across Vaults, so 3 waits its turn while the installation moves.
        steps = STOPPED_SECONDS // SAMPLE_SECONDS + 30
        for i in range(steps):
            self.meter.record(mono=i * 2.0, wall=self.wall0 + i * 2, cut=None,
                              uploads={3: row(), 4: row(held=i * MB)}, verification={})
        mono = (steps - 1) * 2.0
        uploading = {"files": 1, "bytes": 10_000 * MB, "transferred_bytes": MB}
        activity = {"global": summary(), "vaults": [
            self.activity()["vaults"][0],
            {"vault_id": 4, "paused": False, **summary(uploading=uploading)}]}
        result = annotate(activity, meter=self.meter, now=NOW, mono=mono)
        self.assertEqual([vault["stopped_since"] for vault in result["vaults"]], [None, None])
        self.assertIsNone(result["global"]["stopped_since"])
        # A Vault holding a Job that does not move is stopped all the same.
        activity["vaults"][0] = {"vault_id": 3, "paused": False, **summary(uploading=uploading)}
        result = annotate(activity, meter=self.meter, now=NOW, mono=mono)
        self.assertIsNotNone(result["vaults"][0]["stopped_since"])
        self.assertIsNone(result["vaults"][1]["stopped_since"])
        self.assertIsNone(result["global"]["stopped_since"])

    def test_a_vault_never_seen_moving_is_stopped_since_the_sampler_started(self):
        steps = STOPPED_SECONDS // SAMPLE_SECONDS + 10
        for i in range(steps):
            self.sample(i, held=0, vault=9)
        mono = (steps - 1) * 2.0
        result = annotate(self.activity(), meter=self.meter, now=NOW, mono=mono)
        self.assertEqual(result["vaults"][0]["stopped_since"],
                         datetime.fromtimestamp(self.wall0, timezone.utc).isoformat())
        # Without fresh samples nothing is claimed.
        self.assertIsNone(annotate(self.activity(), meter=self.meter, now=NOW,
                                   mono=mono + 60)["vaults"][0]["stopped_since"])


class SampleTests(unittest.TestCase):
    def test_one_bounded_aggregate_per_sample_settles_completions_once(self):
        meter = ThroughputMeter()
        connection = Mock()
        connection.execute.return_value.fetchall.side_effect = [
            [{"vault_id": 3, **row(recent=5 * MB, recent_files=1, held=MB)}],
            [{"vault_id": 3, **row(settled=5 * MB, settled_files=1, held=3 * MB)}],
        ]

        @contextmanager
        def connect():
            yield connection
        status = {3: {"scanning": True, "scan_phase": "verification", "scan_id": "x",
                      "verify_bytes_done": 7 * MB}}
        with patch.dict(throughput.runtime_status, status, clear=True):
            throughput.sample(connect, meter=meter, now=NOW, mono=0.0)
            throughput.sample(connect, meter=meter, now=NOW + timedelta(seconds=2), mono=2.0)
        first, second = [call.args for call in connection.execute.call_args_list]
        query = first[0]
        self.assertTrue(query.strip().startswith("SELECT"))
        self.assertIn("GROUP BY j.vault_id", query)
        self.assertIn("reconcile_only", query)
        cut = NOW - timedelta(seconds=throughput.SETTLE_LAG_SECONDS)
        self.assertEqual(first[1], {"since": cut, "cut": cut})
        self.assertEqual(second[1], {"since": cut, "cut": cut + timedelta(seconds=2)})
        self.assertEqual([sample[2] for sample in meter._vaults[3].samples], [0, 2 * MB])
        self.assertEqual([sample[4] for sample in meter._vaults[3].samples], [0, 0])

    def test_sampler_stops_when_cancelled_and_survives_a_failing_sample(self):
        calls = []

        def failing(*_args, **_kwargs):
            calls.append(threading.current_thread().name)
            raise RuntimeError("database unavailable")

        async def run():
            with patch.object(throughput, "sample", failing), patch.object(throughput, "SAMPLE_SECONDS", 0.01):
                task = asyncio.create_task(throughput.sampler_loop(lambda: None))
                async def three_calls():
                    while len(calls) < 3:
                        self.assertFalse(task.done())
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(three_calls(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with self.assertLogs(throughput.logger, "WARNING") as logs:
            asyncio.run(run())
        self.assertEqual(len(logs.records), 1)  # The same error is logged once.
        self.assertTrue(all(name.startswith("throughput-sampler") for name in calls))


if __name__ == "__main__":
    unittest.main()
