"""Measured speeds of the two adoption phases and one expected finish (#755).

A sampler records, every :data:`SAMPLE_SECONDS`, two monotone counters per
Vault, kept only in memory for :data:`HISTORY_SECONDS` (lost at restart):

- **upload**: the bytes and files of upload Jobs completed since the sampler
  started, plus the bytes the held Jobs already sent (``transferred_bytes``),
  so a large file moves the speed while it is sent, not when it ends;
- **verification**: the bytes the running scan has read from the disk to
  fingerprint its Local Copies (runtime status).

From the samples come the instant speed (last :data:`INSTANT_SECONDS`), the
average used for estimates (last :data:`AVERAGE_SECONDS`, both phases) and a
history for a sparkline. A Vault's expected finish is the later of its two
phases at their average, with the phase that limits it.

The meter also keeps when each Vault last made progress (#759): bytes or files
sent, bytes verified, bytes read back from S3 to verify an upload (#763), or a
running scan moving. A Vault with work that should
move and no progress for :data:`STOPPED_SECONDS` has no expected finish: it is
shown stopped since its last progress instead.

The same sampler records the bytes read from each Source Device under its
read slots (#766), whose instant rate ``GET /api/stats`` reports per device
(``runtime.source_devices[].bytes_per_second``).

Each sample reads bounded SQL aggregates only: the held upload Jobs and those
completed since the previous sample, through their partial indexes. Never
files or Job rows.
"""
from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import threading
import time
from typing import Any, Callable

from .runtime_status import runtime_status, status_lock

SAMPLE_SECONDS = 2
HISTORY_SECONDS = 15 * 60
INSTANT_SECONDS = 20
INSTANT_MIN_SECONDS = 10
AVERAGE_SECONDS = 5 * 60
AVERAGE_MIN_SECONDS = 60
HISTORY_STEP_SECONDS = 30
# A completion is counted as settled only this long after it finished, so a
# completing transaction that commits after the sample is not missed.
SETTLE_LAG_SECONDS = 10
# Samples older than this mean the sampler stopped: no speed is claimed.
STALE_SECONDS = 15
# No progress for this long: the expected finish is unknown, the Vault is
# stopped since its last progress (#759).
STOPPED_SECONDS = 5 * 60
PHASES = ("upload", "verification")
_HELD = ("uploading", "verifying", "cleaning")

# Bytes read back from S3 to verify uploads, per Vault, since start (#763).
# The Job's transferred bytes stay at the file's size while its copy is read
# back, so a long read-back of a large file would otherwise move nothing.
_read_back_lock = threading.Lock()
_read_back_bytes: dict[int, int] = {}


def note_read_back(vault_id: int | None, size: int) -> None:
    """Count one chunk read back from S3 for the Vault's upload verification."""
    if vault_id is None or size <= 0:
        return
    with _read_back_lock:
        _read_back_bytes[int(vault_id)] = _read_back_bytes.get(int(vault_id), 0) + int(size)

logger = logging.getLogger(__name__)

# One query per sample: the held upload Jobs (jobs_held_idx) and those
# completed since the previous cut (jobs_finished_idx). Reconcile-only Jobs
# send nothing. Completions up to the cut are settled into the counter; later
# ones count with the held bytes until the next sample settles them.
_SAMPLE_SQL = f"""
    SELECT j.vault_id,
        count(*) FILTER (WHERE j.status='completed' AND j.finished_at <= %(cut)s) AS settled_files,
        COALESCE(sum(GREATEST(j.total_bytes,0))
            FILTER (WHERE j.status='completed' AND j.finished_at <= %(cut)s),0) AS settled_bytes,
        count(*) FILTER (WHERE j.status='completed' AND j.finished_at > %(cut)s) AS recent_files,
        COALESCE(sum(GREATEST(j.total_bytes,0))
            FILTER (WHERE j.status='completed' AND j.finished_at > %(cut)s),0) AS recent_bytes,
        COALESCE(sum(LEAST(GREATEST(j.transferred_bytes,0),GREATEST(j.total_bytes,0)))
            FILTER (WHERE j.status IN {_HELD}),0) AS held_bytes
    FROM jobs j LEFT JOIN job_uploads u ON u.job_id=j.id
    WHERE j.action='upload' AND NOT COALESCE(u.reconcile_only,false)
        AND (j.status IN {_HELD} OR (j.status='completed' AND j.finished_at > %(since)s))
    GROUP BY j.vault_id
"""


@dataclass
class _Series:
    # (monotonic seconds, wall epoch seconds, upload bytes, upload files, verification bytes)
    samples: deque = field(default_factory=lambda: deque(maxlen=int(HISTORY_SECONDS // SAMPLE_SECONDS) + 1))
    upload_bytes: int = 0
    upload_files: int = 0
    verification_bytes: int = 0
    settled_bytes: int = 0
    settled_files: int = 0
    raw_bytes: int | None = None
    raw_files: int | None = None
    scan_id: str | None = None
    scan_done: int = 0
    # (monotonic, wall) of the last progress, and the running scan's position.
    last_progress: tuple[float, float] | None = None
    scan_marker: tuple | None = None
    working: bool | None = None


class ThroughputMeter:
    """Thread-safe ring buffers of the counters, per Vault and installation-wide."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vaults: dict[int, _Series] = {}
        self._total = _Series()
        self._cut: datetime | None = None
        self._started: tuple[float, float] | None = None
        # The installation's last progress, any Vault (#759).
        self._moved: tuple[float, float] | None = None
        # (monotonic seconds, bytes read) per Source Device (#766).
        self._devices: dict[str, deque] = {}

    def reset(self) -> None:
        with self._lock:
            self._vaults.clear()
            self._devices.clear()
            self._total = _Series()
            self._cut = None
            self._started = None
            self._moved = None

    def window(self, now: datetime) -> tuple[datetime, datetime]:
        """The (since, cut] interval of completions the next sample settles."""
        cut = now - timedelta(seconds=SETTLE_LAG_SECONDS)
        with self._lock:
            since = self._cut or cut
        return since, max(since, cut)

    def record(self, *, mono: float, wall: float, cut: datetime | None,
               uploads: dict[int, dict[str, int]],
               verification: dict[int, tuple[str | None, int]],
               scans: dict[int, tuple] | None = None, held_back: bool = False) -> None:
        """Add one sample. ``uploads`` holds the query row of each Vault with
        upload activity; ``verification`` the running scan id and the bytes it
        verified, for each Vault in the runtime status; ``scans`` a progress
        marker per Vault: the position of its running scan (a scan finalizing
        always moves) and the bytes read back from S3 (#763). ``held_back``
        means Server Priority holds the reads: a choice, not a stop."""
        scans = scans or {}
        with self._lock:
            if cut is not None:
                self._cut = cut
            if self._started is None:
                self._started = (mono, wall)
            moved = {"bytes": 0, "files": 0, "verification": 0}
            for vault_id in set(self._vaults) | set(uploads) | set(verification) | set(scans):
                if vault_id not in self._vaults:
                    # A Vault first seen moves from now, not from the start.
                    self._vaults[vault_id] = _Series(last_progress=(mono, wall))
                series = self._vaults[vault_id]
                row = uploads.get(vault_id) or {}
                series.settled_bytes += int(row.get("settled_bytes") or 0)
                series.settled_files += int(row.get("settled_files") or 0)
                raw_bytes = series.settled_bytes + int(row.get("recent_bytes") or 0) + int(row.get("held_bytes") or 0)
                raw_files = series.settled_files + int(row.get("recent_files") or 0)
                # A failed or restarted send lowers the raw sum: never a negative speed.
                step_bytes = max(0, raw_bytes - series.raw_bytes) if series.raw_bytes is not None else 0
                step_files = max(0, raw_files - series.raw_files) if series.raw_files is not None else 0
                series.raw_bytes, series.raw_files = raw_bytes, raw_files
                scan_id, done = verification.get(vault_id, (None, 0))
                step_verified = 0
                if scan_id is not None:
                    previous = series.scan_done if scan_id == series.scan_id else 0
                    step_verified = max(0, int(done) - previous) if series.samples else 0
                    series.scan_id, series.scan_done = scan_id, int(done)
                else:
                    series.scan_id, series.scan_done = None, 0
                marker = scans.get(vault_id)
                scan_moved = marker is not None and (
                    marker != series.scan_marker or "finalization" in marker)
                series.scan_marker = marker
                if held_back or step_bytes or step_files or step_verified or scan_moved:
                    series.last_progress = (mono, wall)
                    self._moved = (mono, wall)
                series.upload_bytes += step_bytes
                series.upload_files += step_files
                series.verification_bytes += step_verified
                series.samples.append((mono, wall, series.upload_bytes, series.upload_files,
                                       series.verification_bytes))
                moved["bytes"] += step_bytes
                moved["files"] += step_files
                moved["verification"] += step_verified
            total = self._total
            total.upload_bytes += moved["bytes"]
            total.upload_files += moved["files"]
            total.verification_bytes += moved["verification"]
            total.samples.append((mono, wall, total.upload_bytes, total.upload_files,
                                  total.verification_bytes))

    def record_devices(self, *, mono: float, totals: dict[str, int]) -> None:
        """Add one sample of the bytes read per Source Device (#766)."""
        with self._lock:
            for device, total in totals.items():
                series = self._devices.get(device)
                if series is None:
                    if len(self._devices) >= 64:
                        continue  # Bounded: a device first seen beyond it waits.
                    series = self._devices[device] = deque(
                        maxlen=int(INSTANT_SECONDS * 2 // SAMPLE_SECONDS) + 2)
                series.append((mono, int(total)))

    def device_rates(self, *, mono: float | None = None) -> dict[str, int | None]:
        """The instant read rate of each sampled device, None when unmeasured."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            devices = {device: list(series) for device, series in self._devices.items()}
        rates: dict[str, int | None] = {}
        for device, samples in devices.items():
            rate = None
            if len(samples) >= 2 and mono - samples[-1][0] <= STALE_SECONDS:
                rate = _speed(samples, 1, window=INSTANT_SECONDS, min_span=INSTANT_MIN_SECONDS,
                              trim_idle=False)
            rates[device] = None if rate is None else round(max(0.0, rate))
        return rates

    def stopped_since(self, vault_id: int, *, working: bool, mono: float,
                      in_line: bool = False) -> str | None:
        """When a Vault with work that should move last made progress, once
        that is :data:`STOPPED_SECONDS` ago; else None (#759).

        Work that appears after the Vault was seen idle starts its clock.
        A Vault whose only work waits in the queue (``in_line``) is not
        stopped while the installation moves: Jobs are claimed in order
        across Vaults, and retries wait out their backoff. Without fresh
        samples nothing is claimed.
        """
        with self._lock:
            series = self._vaults.get(int(vault_id))
            if series is None:
                if self._started is None:
                    return None
                series = self._vaults[int(vault_id)] = _Series(last_progress=self._started)
            if working and series.working is False:
                series.last_progress = (mono, time.time())
            series.working = working
            latest = self._total.samples[-1][0] if self._total.samples else None
            last = series.last_progress
            moved = self._moved
        if (not working or last is None or latest is None or mono - latest > STALE_SECONDS
                or mono - last[0] < STOPPED_SECONDS):
            return None
        if in_line and moved is not None and mono - moved[0] < STOPPED_SECONDS:
            return None
        return datetime.fromtimestamp(last[1], timezone.utc).isoformat()

    def snapshot(self, vault_id: int | None, *, mono: float | None = None) -> dict[str, Any]:
        """Speeds of one Vault, or of the installation with ``vault_id=None``."""
        with self._lock:
            series = self._total if vault_id is None else self._vaults.get(int(vault_id))
            samples = list(series.samples) if series is not None else []
        return speeds(samples, mono=time.monotonic() if mono is None else mono)


def _speed(samples: list[tuple], index: int, *, window: float, min_span: float,
           trim_idle: bool) -> float | None:
    last = samples[-1]
    times = [sample[0] for sample in samples]
    start = bisect_right(times, last[0] - window)
    if start > 0 and last[0] - times[start - 1] <= window + SAMPLE_SECONDS / 2:
        start -= 1  # The sample at the window's edge, not the one after it.
    if start >= len(samples) - 1:
        return None
    if trim_idle:
        # Measure an average from where the phase began moving: an idle
        # prefix is not part of its pace.
        while start < len(samples) - 1 and samples[start + 1][index] == samples[start][index]:
            start += 1
    span = last[0] - samples[start][0]
    if span < min_span:
        return None
    return (last[index] - samples[start][index]) / span


def _history(samples: list[tuple]) -> list[dict[str, Any]]:
    if len(samples) < 2:
        return []
    times = [sample[0] for sample in samples]
    end = samples[-1][0]
    points: list[dict[str, Any]] = []
    later = samples[-1]
    for step in range(1, HISTORY_SECONDS // HISTORY_STEP_SECONDS + 1):
        position = bisect_right(times, end - step * HISTORY_STEP_SECONDS) - 1
        if position < 0:
            position = 0
        earlier = samples[position]
        span = later[0] - earlier[0]
        if span <= 0:
            break
        points.append({
            "at": datetime.fromtimestamp(later[1], timezone.utc).isoformat(),
            "upload_bytes_per_second": round((later[2] - earlier[2]) / span),
            "verification_bytes_per_second": round((later[4] - earlier[4]) / span),
        })
        if position == 0:
            break
        later = earlier
    points.reverse()
    return points


def speeds(samples: list[tuple], *, mono: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sample_seconds": SAMPLE_SECONDS, "instant_seconds": INSTANT_SECONDS,
        "average_seconds": AVERAGE_SECONDS, "history_seconds": HISTORY_SECONDS,
        "upload": {"bytes_per_second": None, "files_per_second": None, "average_bytes_per_second": None},
        "verification": {"bytes_per_second": None, "average_bytes_per_second": None},
        "history": [],
    }
    if len(samples) < 2 or mono - samples[-1][0] > STALE_SECONDS:
        return result

    def measure(index: int, *, average: bool) -> float | None:
        return _speed(samples, index, window=AVERAGE_SECONDS if average else INSTANT_SECONDS,
                      min_span=AVERAGE_MIN_SECONDS if average else INSTANT_MIN_SECONDS,
                      trim_idle=average)

    def rounded(value: float | None, digits: int = 0) -> float | int | None:
        return None if value is None else (round(value, digits) if digits else round(value))

    result["upload"] = {
        "bytes_per_second": rounded(measure(2, average=False)),
        "files_per_second": rounded(measure(3, average=False), 2),
        "average_bytes_per_second": rounded(measure(2, average=True)),
    }
    result["verification"] = {
        "bytes_per_second": rounded(measure(4, average=False)),
        "average_bytes_per_second": rounded(measure(4, average=True)),
    }
    result["history"] = _history(samples)
    return result


def upload_bytes_left(summary: dict[str, Any]) -> int:
    """Bytes still to send: waiting and queued files, and what held sends lack."""
    uploading = summary.get("uploading") or {}
    return (int((summary.get("stability_wait") or {}).get("bytes") or 0)
            + int((summary.get("queued") or {}).get("bytes") or 0)
            + max(0, int(uploading.get("bytes") or 0) - int(uploading.get("transferred_bytes") or 0)))


def expected_finish(*, now: datetime, upload_left: int, upload_average: float | None,
                    verification_left: int, verification_average: float | None) -> dict[str, Any] | None:
    """The later of the active phases at their average, and the phase that
    limits it; None while an active phase has no measured average."""
    phases = [(left, average, name) for left, average, name in (
        (upload_left, upload_average, "upload"),
        (verification_left, verification_average, "verification")) if left > 0]
    if not phases or any(not average or average <= 0 for _, average, _ in phases):
        return None
    seconds, phase = max((left / average, name) for left, average, name in phases)
    return {"at": (now + timedelta(seconds=seconds)).isoformat(), "seconds": round(seconds),
            "limited_by": phase}


def _verification_left(vault_id: int) -> int:
    with status_lock:
        status = runtime_status.get(int(vault_id)) or {}
        if not status.get("scanning") or status.get("scan_phase") != "verification":
            return 0
        total, done = status.get("verify_bytes_total"), status.get("verify_bytes_done")
    return max(0, int(total or 0) - int(done or 0))


def _should_move(vault: dict[str, Any], verification_left: int) -> bool:
    """Work that should be moving: files to upload or verify, or a disk read.
    Files still waiting to be stable wait by design."""
    return verification_left > 0 or any(
        int((vault.get(stage) or {}).get("files") or 0) > 0
        for stage in ("queued", "uploading", "verifying"))


def annotate(activity: dict[str, Any], *, meter: "ThroughputMeter | None" = None,
             now: datetime | None = None, mono: float | None = None) -> dict[str, Any]:
    """Add each Vault's and the installation's speeds and expected finish,
    or since when a Vault with work has not moved (#759)."""
    meter = meter or METER
    now = now or datetime.now(timezone.utc)
    mono = time.monotonic() if mono is None else mono
    finishes: list[dict[str, Any] | None] = []
    stops: list[str | None] = []
    for vault in activity.get("vaults") or []:
        measured = meter.snapshot(int(vault["vault_id"]), mono=mono)
        vault["throughput"] = measured
        upload_left = 0 if vault.get("paused") else upload_bytes_left(vault)
        verification_left = _verification_left(int(vault["vault_id"]))
        working = not vault.get("paused") and _should_move(vault, verification_left)
        in_line = verification_left <= 0 and not any(
            int((vault.get(stage) or {}).get("files") or 0) > 0 for stage in ("uploading", "verifying"))
        stopped = meter.stopped_since(int(vault["vault_id"]), working=working, mono=mono,
                                      in_line=in_line)
        finish = None if vault.get("paused") or stopped else expected_finish(
            now=now, upload_left=upload_left,
            upload_average=measured["upload"]["average_bytes_per_second"],
            verification_left=verification_left,
            verification_average=measured["verification"]["average_bytes_per_second"])
        vault["expected_finish"] = finish
        vault["stopped_since"] = stopped
        if not vault.get("paused") and (upload_left > 0 or verification_left > 0):
            finishes.append(finish)
        if working:
            stops.append(stopped)
    total = activity.get("global")
    if isinstance(total, dict):
        total["throughput"] = meter.snapshot(None, mono=mono)
        # The installation stops when every Vault with work has stopped, at
        # the last progress among them.
        total["stopped_since"] = max(stops) if stops and all(stops) else None  # type: ignore[type-var]
        # The installation finishes with its last Vault; unknown while any
        # working Vault's finish is.
        total["expected_finish"] = (max(finishes, key=lambda finish: finish["seconds"])
                                    if finishes and all(finishes) and not total["stopped_since"]
                                    else None)
    return activity


def sample(connect: Callable[[], Any], *, meter: "ThroughputMeter | None" = None,
           now: datetime | None = None, mono: float | None = None,
           held_back: bool | None = None) -> None:
    """Record one sample: one bounded aggregate query plus the runtime status."""
    meter = meter or METER
    now = now or datetime.now(timezone.utc)
    since, cut = meter.window(now)
    with connect() as connection:
        rows = connection.execute(_SAMPLE_SQL, {"since": since, "cut": cut}).fetchall()
    uploads = {int(row["vault_id"]): dict(row) for row in rows}
    with status_lock:
        verification = {
            int(vault_id): (status.get("scan_id"), int(status.get("verify_bytes_done") or 0))
            if status.get("scanning") and status.get("scan_phase") == "verification" else (None, 0)
            for vault_id, status in runtime_status.items()}
        # Where each running scan is: a scan walking or finalizing moves too.
        scans = {
            int(vault_id): (status.get("scan_id"), status.get("scan_phase"),
                            status.get("traversal_files_seen"), status.get("verify_bytes_done"),
                            status.get("verify_files_done"), status.get("verify_bytes_read"))
            for vault_id, status in runtime_status.items() if status.get("scanning")}
    # A read-back moving counts as progress too (#763).
    with _read_back_lock:
        read_back = dict(_read_back_bytes)
    for vault_id, read in read_back.items():
        scans[vault_id] = (*scans.get(vault_id, ()), "read_back", read)
    if held_back is None:
        from .source_devices import throttled_if_started
        held_back = throttled_if_started()
    mono = time.monotonic() if mono is None else mono
    meter.record(mono=mono, wall=now.timestamp(),
                 cut=cut, uploads=uploads, verification=verification, scans=scans,
                 held_back=held_back)
    from .source_devices import read_totals
    meter.record_devices(mono=mono, totals=read_totals())


METER = ThroughputMeter()


async def sampler_loop(connect: Callable[[], Any]) -> None:
    """Sample until cancelled at shutdown, on a dedicated DB-only executor."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="throughput-sampler")
    METER.reset()
    last_error = ""
    try:
        while True:
            try:
                await loop.run_in_executor(executor, sample, connect)
                last_error = ""
            except Exception as exc:  # Informational only: log a new error once.
                message = f"{type(exc).__name__}: {exc}"
                if message != last_error:
                    logger.warning("Throughput sample failed: %s", message)
                last_error = message
            await asyncio.sleep(SAMPLE_SECONDS)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
