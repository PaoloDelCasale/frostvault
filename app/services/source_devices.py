"""Reads of Local Copies per Source Device (#713, ADR-0046).

The Source Device of a Local Copy is the disk its bytes are read from. On an
Unraid user share (FUSE ``shfs``) it is the array disk named by the extended
attribute ``system.LOCATION`` (``disk1``, ``disk2``, ``cache``); elsewhere it
is the ``st_dev`` of the file. A guess is cached per parent folder, because a
folder's files mostly sit on one disk. A wrong guess only costs scheduling
efficiency: the device never decides what is read or published, and looking
it up never fails an operation.

One process-wide registry bounds the reads in flight on each device, in
addition to the global limits of scan verification and uploads, and shared by
both: ``SOURCE_DEVICE_READ_CONCURRENCY`` when set, for every device;
otherwise 1 on a rotational disk and 3 on a solid-state or unknown one, read
from ``/sys/block/<disk>/queue/rotational`` (#766). A slot is held only
while the body of a Local Copy is read. Waiting for one runs the caller's
checkpoint every :data:`RECHECK_SECONDS`, so cancellation, shutdown and claim
loss still win while queued.

The optional server priority (``SERVER_PRIORITY_IOWAIT_PERCENT``) samples the
host's iowait from ``/proc/stat``. Above the threshold for
:data:`THROTTLE_AFTER_SECONDS` FrostVault slows down: one read per device and
no new upload Jobs; it recovers after :data:`RECOVER_AFTER_SECONDS` below.

The registry also counts the bytes read under each slot, so the sampler of
:mod:`throughput` reports the read rate of each device (#766).
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping

LOGGER = logging.getLogger(__name__)

LOCATION_XATTR = "system.LOCATION"
# Folders whose device is remembered; one entry per folder, not per file.
DIRECTORY_CACHE_SIZE = 4096
# Ready Jobs the claim looks at to find one on a device with a free slot.
CLAIM_WINDOW = 64
# A Local Copy the scan just hashed is likely still in the page cache.
HOT_TTL_SECONDS = 300.0
HOT_LIMIT = 1024
RECHECK_SECONDS = 0.5
# Server priority hysteresis.
SAMPLE_SECONDS = 2.0
THROTTLE_AFTER_SECONDS = 10.0
RECOVER_AFTER_SECONDS = 30.0
PROC_STAT = "/proc/stat"
# Default reads per device when SOURCE_DEVICE_READ_CONCURRENCY is unset (#766):
# beyond one stream a rotational disk seeks more than it reads.
ROTATIONAL_LIMIT = 1
SOLID_STATE_LIMIT = 3
SYS_ROOT = "/sys"
PROC_MDSTAT = "/proc/mdstat"
# Local Copies whose fingerprint an upload has just published (#766).
UPLOAD_FINGERPRINTS_LIMIT = 65536
UPLOAD_FINGERPRINTS_TTL_SECONDS = 24 * 3600.0


# -- Source Device of a path -----------------------------------------------

class DeviceResolver:
    """``path`` -> an opaque device key, cached per parent folder (LRU)."""

    def __init__(self, *, getxattr: Callable[..., bytes] | None = None,
                 stat: Callable[..., os.stat_result] = os.stat,
                 size: int = DIRECTORY_CACHE_SIZE):
        if getxattr is None and hasattr(os, "getxattr"):
            # The attribute of the entry itself, never of a symlink's target.
            getxattr = lambda path, name: os.getxattr(path, name, follow_symlinks=False)  # noqa: E731
        self._getxattr = getxattr
        self._stat = stat
        self._size = max(1, int(size))
        self._lock = threading.Lock()
        self._folders: OrderedDict[str, str] = OrderedDict()

    def device_of(self, path: Any) -> str:
        path = os.fspath(path)
        folder = os.path.dirname(path)
        with self._lock:
            device = self._folders.get(folder)
            if device is not None:
                self._folders.move_to_end(folder)
                return device
        device = self._lookup(path)
        if device is None:
            # Not remembered: the next file of the folder may answer.
            return "unknown"
        with self._lock:
            self._folders[folder] = device
            self._folders.move_to_end(folder)
            while len(self._folders) > self._size:
                self._folders.popitem(last=False)
        return device

    def _lookup(self, path: str) -> str | None:
        if self._getxattr is not None:
            try:
                # Metadata only: the attribute is answered without reading data.
                value = self._getxattr(path, LOCATION_XATTR)
            except (OSError, ValueError, TypeError):
                value = None
            if value:
                name = bytes(value).split(b"\0", 1)[0].strip().decode("utf-8", "replace")
                if name:
                    return f"location:{name}"
        try:
            return f"dev:{self._stat(path, follow_symlinks=False).st_dev}"
        except (OSError, ValueError, TypeError):
            return None

    def clear(self) -> None:
        with self._lock:
            self._folders.clear()


# -- Rotational or solid-state ------------------------------------------------

_ARRAY_DISK = re.compile(r"disk([0-9]+)")


class RotationalProbe:
    """Whether the disk behind a device key is rotational (#766).

    ``dev:<st_dev>`` is followed through ``<sys>/dev/block/<major>:<minor>``,
    from a partition to its disk, and through the ``slaves`` of a dm/md
    device (rotational when any member is). ``location:diskN`` (an Unraid
    array disk) reads ``<sys>/block/mdNp1`` (``mdN`` on older releases), then
    the member named by ``rdevName.N`` in ``/proc/mdstat``. Anything else, a
    pool such as ``cache`` or a FUSE ``st_dev``, is unknown: None. Never
    raises; answers are cached per key.
    """

    def __init__(self, *, sys_root: str = SYS_ROOT, mdstat: str = PROC_MDSTAT):
        self._sys, self._mdstat = os.fspath(sys_root), os.fspath(mdstat)
        self._lock = threading.Lock()
        self._known: dict[str, bool | None] = {}

    def rotational(self, device: str | None) -> bool | None:
        if not device:
            return None
        with self._lock:
            if device in self._known:
                return self._known[device]
        try:
            answer = self._lookup(device)
        except Exception:  # A broken sysfs never stops a read.
            answer = None
        with self._lock:
            if len(self._known) >= DIRECTORY_CACHE_SIZE:
                self._known.clear()
            self._known[device] = answer
        return answer

    def _lookup(self, device: str) -> bool | None:
        kind, _, name = device.partition(":")
        if kind == "dev":
            number = int(name)
            path = os.path.join(self._sys, "dev", "block", f"{os.major(number)}:{os.minor(number)}")
            return self._block(path) if os.path.exists(path) else None
        if kind == "location":
            match = _ARRAY_DISK.fullmatch(name)
            if match is None:
                return None
            index = match.group(1)
            for candidate in (f"md{index}p1", f"md{index}"):
                path = os.path.join(self._sys, "block", candidate)
                if os.path.exists(path):
                    answer = self._block(path)
                    if answer is not None:
                        return answer
            member = self._mdstat_member(index)
            if member:
                path = os.path.join(self._sys, "block", member)
                if os.path.exists(path):
                    return self._block(path)
        return None

    def _block(self, path: str, depth: int = 0) -> bool | None:
        if depth > 8:
            return None
        real = os.path.realpath(path)
        if os.path.exists(os.path.join(real, "partition")):
            real = os.path.dirname(real)
        slaves = os.path.join(real, "slaves")
        members = sorted(os.listdir(slaves)) if os.path.isdir(slaves) else []
        if members:
            answers = [self._block(os.path.join(slaves, member), depth + 1) for member in members]
            if any(answer is True for answer in answers):
                return True
            return False if all(answer is False for answer in answers) else None
        try:
            with open(os.path.join(real, "queue", "rotational"), encoding="ascii") as handle:
                value = handle.read(8).strip()
        except (OSError, ValueError):
            return None
        return {"1": True, "0": False}.get(value)

    def _mdstat_member(self, index: str) -> str | None:
        try:
            with open(self._mdstat, encoding="ascii", errors="replace") as handle:
                text = handle.read(65536)
        except OSError:
            return None
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == f"rdevName.{index}":
                value = value.strip()
                # A bare kernel name only, never a path.
                return value if value and "/" not in value and value not in {".", ".."} else None
        return None


def _per_device(limit: int | Callable[..., int]) -> Callable[[str | None], int]:
    """A limit that may or may not depend on the device, as ``f(device)``."""
    if not callable(limit):
        value = int(limit)
        return lambda device=None: value
    try:
        parameters = inspect.signature(limit).parameters.values()
    except (TypeError, ValueError):
        return lambda device=None: limit()
    takes = any(parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD,
                                   parameter.VAR_POSITIONAL) for parameter in parameters)
    return (lambda device=None: limit(device)) if takes else (lambda device=None: limit())


# -- Per-device read slots --------------------------------------------------

# The device whose slot the current thread holds, for :func:`note_read`.
_current = threading.local()

class DeviceSlots:
    """Reads in flight per device; one shared instance per process."""

    def __init__(self, limit: int | Callable[..., int]):
        # ``limit(device)`` or ``limit()``: reads allowed on that device.
        self._limit = _per_device(limit)
        self._condition = threading.Condition()
        self._reading: Counter[str] = Counter()
        self._waiting: Counter[str] = Counter()
        # Bytes read under a slot since start, per device (#766).
        self._read_bytes: Counter[str] = Counter()

    def limit(self, device: str | None = None) -> int:
        try:
            return max(1, int(self._limit(device)))
        except Exception:  # A broken setting never stops a read.
            return 1

    def note_read(self, device: str, size: int) -> None:
        if size > 0:
            with self._condition:
                self._read_bytes[device] += int(size)

    def read_totals(self) -> dict[str, int]:
        """Bytes read per device since start (monotone counters)."""
        with self._condition:
            return dict(self._read_bytes)

    def load(self, device: str) -> int:
        """Reads running plus reads waiting on ``device``."""
        with self._condition:
            return self._reading[device] + self._waiting[device]

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    @contextmanager
    def reading(self, device: str, check: Callable[[], None] | None = None) -> Iterator[None]:
        acquired = False
        with self._condition:
            self._waiting[device] += 1
        try:
            while True:
                # Never call the caller's checkpoint while holding the mutex.
                if check is not None:
                    check()
                with self._condition:
                    if self._reading[device] < self.limit(device):
                        self._reading[device] += 1
                        acquired = True
                        break
                    self._condition.wait(RECHECK_SECONDS)
        finally:
            with self._condition:
                self._waiting[device] -= 1
                if not self._waiting[device]:
                    del self._waiting[device]
        previous = getattr(_current, "entry", None)
        _current.entry = (self, device)
        try:
            yield
        finally:
            _current.entry = previous
            with self._condition:
                if acquired:
                    self._reading[device] -= 1
                    if not self._reading[device]:
                        del self._reading[device]
                self._condition.notify_all()

    def snapshot(self, rates: Mapping[str, int | None] | None = None) -> list[dict[str, Any]]:
        """Bounded per-device counts for the runtime status (observability).

        A device is listed while it has reads running or waiting, or a
        measured read rate above zero (``rates``, from :mod:`throughput`).
        """
        rates = rates or {}
        with self._condition:
            active = set(self._reading) | set(self._waiting)
            devices = sorted(active | {device for device, rate in rates.items() if rate})
            counts = [(device, self._reading[device], self._waiting[device])
                      for device in devices[:32]]
        # The limit may read sysfs: never under the mutex.
        return [{"device": device.split(":", 1)[-1], "reading": reading, "waiting": waiting,
                 "limit": self.limit(device), "bytes_per_second": rates.get(device)}
                for device, reading, waiting in counts]


def note_read(size: int) -> None:
    """Count ``size`` bytes read under the slot the current thread holds.

    Called from the read loops of the scan verification, the Upload Snapshot
    pass and the send; a read outside any slot is not counted.
    """
    entry = getattr(_current, "entry", None)
    if entry is not None:
        slots_, device = entry
        slots_.note_read(device, size)


# -- Local Copies an upload has just fingerprinted ------------------------------

class UploadFingerprints:
    """Observations whose fingerprint an upload's whole read published (#766).

    Keyed by (file id, generation, size, mtime): the scan verification skips
    a row of its listing only for that very observation, whose digest the
    same compare-and-swap as its own already published. An entry is used by
    one skip (a later listing of the same observation means its fingerprint
    was cleared since: it is read) and dropped when the upload retires the
    fingerprint. Process-local and bounded; forgetting one only costs a read.
    """

    def __init__(self, *, ttl: float = UPLOAD_FINGERPRINTS_TTL_SECONDS,
                 limit: int = UPLOAD_FINGERPRINTS_LIMIT,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl, self._limit, self._clock = float(ttl), max(1, int(limit)), clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, int, int, int], float] = OrderedDict()

    @staticmethod
    def _key(file_id: Any, generation: Any, size: Any, mtime_ns: Any) -> tuple[str, int, int, int] | None:
        try:
            return str(file_id), int(generation), int(size), int(mtime_ns)
        except (TypeError, ValueError):
            return None

    def add(self, file_id: Any, *, generation: Any, size: Any, mtime_ns: Any) -> None:
        key = self._key(file_id, generation, size, mtime_ns)
        if key is None:
            return
        with self._lock:
            self._entries[key] = self._clock()
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def discard(self, file_id: Any) -> None:
        """Forget every observation of ``file_id`` (its fingerprint was retired)."""
        with self._lock:
            for key in [key for key in self._entries if key[0] == str(file_id)]:
                del self._entries[key]

    def claim(self, row: Mapping[str, Any]) -> bool:
        """Whether the scan row ``row`` is an observation an upload fingerprinted;
        True once per publication."""
        key = self._key(row.get("vault_file_id"), row.get("generation"),
                        row.get("size"), row.get("mtime_ns"))
        if key is None:
            return False
        with self._lock:
            added = self._entries.pop(key, None)
        return added is not None and self._clock() - added < self._ttl


# -- Local Copies the scan just read ----------------------------------------

class HotFiles:
    """File ids hashed moments ago: their upload reads from the page cache."""

    def __init__(self, *, ttl: float = HOT_TTL_SECONDS, limit: int = HOT_LIMIT,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl, self._limit, self._clock = float(ttl), max(1, int(limit)), clock
        self._lock = threading.Lock()
        self._files: OrderedDict[str, float] = OrderedDict()

    def _expire(self, now: float) -> None:
        while self._files:
            file_id, added = next(iter(self._files.items()))
            if now - added < self._ttl:
                break
            del self._files[file_id]

    def add(self, file_id: Any) -> None:
        now = self._clock()
        with self._lock:
            self._files[str(file_id)] = now
            self._files.move_to_end(str(file_id))
            while len(self._files) > self._limit:
                self._files.popitem(last=False)
            self._expire(now)

    def discard(self, file_id: Any) -> None:
        with self._lock:
            self._files.pop(str(file_id), None)

    def __contains__(self, file_id: Any) -> bool:
        with self._lock:
            self._expire(self._clock())
            return str(file_id) in self._files

    def ids(self) -> list[str]:
        with self._lock:
            self._expire(self._clock())
            return list(self._files)


# -- Claim rotation -----------------------------------------------------------

def _is_upload(job: Mapping[str, Any]) -> bool:
    return job.get("action") == "upload"


def rotate_claims(jobs: Iterable[Mapping[str, Any]], *,
                  device_of: Callable[[Mapping[str, Any]], str | None],
                  slots: DeviceSlots, hot: HotFiles,
                  window: int = CLAIM_WINDOW) -> Iterator[Mapping[str, Any]]:
    """Yield ``jobs`` in claim order, rotating uploads across devices.

    Each step looks at the uploads ahead of the next other Job: first an
    upload whose Local Copy the scan has just read (it jumps ahead); then,
    within ``window`` Jobs, the first upload whose device has a free slot,
    counting the reads in flight and the uploads already yielded by this
    pass; otherwise the current order. A Job that is not an upload is never
    overtaken. ``device_of`` returning None means unknown: always free.
    """
    remaining = list(jobs)
    picked: Counter[str] = Counter()
    while remaining:
        uploads = 0
        while uploads < len(remaining) and _is_upload(remaining[uploads]):
            uploads += 1
        choice = next((index for index in range(uploads)
                       if remaining[index].get("file_id") is not None
                       and remaining[index]["file_id"] in hot), None)
        if choice is None:
            for index in range(min(len(remaining), max(1, int(window)))):
                job = remaining[index]
                if not _is_upload(job):
                    choice = index
                    break
                device = _safe_device(device_of, job)
                if device is None or slots.load(device) + picked[device] < slots.limit(device):
                    choice = index
                    break
        job = remaining.pop(choice or 0)
        if _is_upload(job):
            device = _safe_device(device_of, job)
            if device is not None:
                picked[device] += 1
            if job.get("file_id") is not None:
                hot.discard(job["file_id"])
        yield job


def _safe_device(device_of: Callable[[Mapping[str, Any]], str | None],
                 job: Mapping[str, Any]) -> str | None:
    try:
        return device_of(job)
    except Exception:
        return None


def interleave_by_device(items: Iterable[Any], *, device_of: Callable[[Any], str],
                         busy: Callable[[str], int], limit: Callable[..., int],
                         lookahead: int = 8192, fill: int = 256) -> Iterator[tuple[str, Any] | None]:
    """Feed scan reads round-robin across devices (#713).

    Keeps up to ``lookahead`` items grouped by device (looking up at most
    ``fill`` more per step, so the first read starts at once) and yields
    ``(device, item)`` from the device after the last one served whose
    ``busy(device)`` (reads this scan has submitted and not finished) is below
    ``limit(device)`` (or ``limit()``), so a busy disk never starves the others. Yields None when
    every buffered device is at its limit: the caller waits for a read to
    finish, then resumes the generator.
    """
    limit_of = _per_device(limit)
    source = iter(items)
    queues: OrderedDict[str, list[Any]] = OrderedDict()
    buffered = 0
    exhausted = False
    last: str | None = None
    while True:
        taken = 0
        while not exhausted and buffered < lookahead and taken < fill:
            taken += 1
            try:
                item = next(source)
            except StopIteration:
                exhausted = True
                break
            queues.setdefault(device_of(item), []).append(item)
            buffered += 1
        if not queues:
            return
        devices = list(queues)
        if last in queues:
            start = devices.index(last) + 1
            devices = devices[start:] + devices[:start]
        device = next((name for name in devices if busy(name) < limit_of(name)), None)
        if device is None:
            yield None
            continue
        queue = queues[device]
        item = queue.pop(0)
        buffered -= 1
        if not queue:
            del queues[device]
        last = device
        yield device, item


# -- Server priority ------------------------------------------------------------

def parse_cpu_times(text: str) -> tuple[int, int] | None:
    """(iowait, total) jiffies of the aggregate ``cpu`` line of /proc/stat."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            try:
                values = [int(value) for value in fields[1:9]]
            except ValueError:
                return None
            if len(values) < 5:
                return None
            # user nice system idle iowait irq softirq steal; guest time is
            # already counted in user/nice.
            return values[4], sum(values)
    return None


class ServerPriority:
    """Hysteresis on the host iowait; disabled when ``threshold`` is 0."""

    def __init__(self, threshold: float, *, read_stat: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 on_change: Callable[[bool], None] | None = None):
        self.threshold = max(0.0, float(threshold or 0))
        self._read_stat = read_stat or _read_proc_stat
        self._clock, self._wall_clock, self._on_change = clock, wall_clock, on_change
        self._lock = threading.Lock()
        self._previous: tuple[float, int, int] | None = None
        self._above_since: float | None = None
        self._below_since: float | None = None
        self.throttled = False
        self.since: str | None = None
        self.iowait_percent: float | None = None
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    def sample(self) -> bool:
        """Take one /proc/stat sample and update the state; returns throttled."""
        if not self.enabled:
            return False
        try:
            times = parse_cpu_times(self._read_stat())
        except OSError:
            times = None
        now = self._clock()
        changed = None
        with self._lock:
            if times is None:
                return self.throttled
            previous, self._previous = self._previous, (now, *times)
            if previous is None:
                return self.throttled
            started, iowait, total = previous
            spent = times[1] - total
            if spent <= 0:
                return self.throttled
            percent = 100.0 * max(0, times[0] - iowait) / spent
            self.iowait_percent = round(percent, 1)
            if percent > self.threshold:
                self._below_since = None
                if self._above_since is None:
                    self._above_since = started
                if not self.throttled and now - self._above_since >= THROTTLE_AFTER_SECONDS:
                    changed = True
            else:
                self._above_since = None
                if self._below_since is None:
                    self._below_since = started
                if self.throttled and now - self._below_since >= RECOVER_AFTER_SECONDS:
                    changed = False
            if changed is not None:
                self.throttled = changed
                self.since = self._wall_clock().isoformat() if changed else None
                self._above_since = self._below_since = None
        if changed is not None:
            if changed:
                LOGGER.warning("Server priority: host iowait %.1f%% above %.0f%% for %ss; "
                               "one read per source device and no new uploads",
                               percent, self.threshold, int(THROTTLE_AFTER_SECONDS))
            else:
                LOGGER.warning("Server priority: host iowait below %.0f%% for %ss; "
                               "normal reads and uploads resume",
                               self.threshold, int(RECOVER_AFTER_SECONDS))
            if self._on_change is not None:
                self._on_change(changed)
        return self.throttled

    def start(self, should_stop: Callable[[], bool]) -> None:
        """Sample in a daemon thread while enabled; idempotent."""
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            def run() -> None:
                while not should_stop():
                    try:
                        self.sample()
                    except Exception:
                        LOGGER.exception("Server priority sample failed")
                    time.sleep(SAMPLE_SECONDS)

            self._thread = threading.Thread(target=run, name="server-priority", daemon=True)
            self._thread.start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "throttled": self.throttled, "since": self.since}


def _read_proc_stat() -> str:
    with open(PROC_STAT, encoding="ascii", errors="replace") as handle:
        return handle.read(4096)


# -- Process-wide registry --------------------------------------------------------

_lock = threading.Lock()
_resolver = DeviceResolver()
_hot = HotFiles()
_probe = RotationalProbe()
_upload_fingerprints = UploadFingerprints()
_slots: DeviceSlots | None = None
_priority: ServerPriority | None = None


def _settings() -> Any:
    from ..config import settings
    return settings


def explicit_limit() -> int | None:
    """``SOURCE_DEVICE_READ_CONCURRENCY`` when set: it applies to every device."""
    value = getattr(_settings(), "source_device_read_concurrency", None)
    return None if value is None else max(1, min(16, int(value)))


def rotational(device: str | None) -> bool | None:
    return _probe.rotational(device)


def configured_limit(device: str | None = None) -> int:
    """Reads in flight allowed on ``device`` (#766).

    The setting wins for every device; otherwise 1 on a rotational disk and 3
    on a solid-state or unknown one (also for ``device=None``).
    """
    explicit = explicit_limit()
    if explicit is not None:
        return explicit
    return ROTATIONAL_LIMIT if rotational(device) is True else SOLID_STATE_LIMIT


def server_priority() -> ServerPriority:
    global _priority
    with _lock:
        if _priority is None:
            threshold = getattr(_settings(), "server_priority_iowait_percent", 0) or 0
            _priority = ServerPriority(threshold, on_change=lambda _throttled: _wake())
            from .shutdown import shutdown_state
            _priority.start(lambda: shutdown_state.requested)
        return _priority


def throttled() -> bool:
    return server_priority().throttled


def throttled_if_started() -> bool:
    """Whether reads are held back, without starting the load monitor."""
    with _lock:
        priority = _priority
    return bool(priority is not None and priority.throttled)


def _current_limit(device: str | None = None) -> int:
    return 1 if throttled() else configured_limit(device)


def slots() -> DeviceSlots:
    global _slots
    with _lock:
        if _slots is None:
            _slots = DeviceSlots(_current_limit)
        return _slots


def read_totals() -> dict[str, int]:
    """Bytes read per device since start; empty before any read slot."""
    with _lock:
        current = _slots
    return current.read_totals() if current is not None else {}


def _wake() -> None:
    with _lock:
        current = _slots
    if current is not None:
        current.wake()


def hot_files() -> HotFiles:
    return _hot


def upload_fingerprints() -> UploadFingerprints:
    return _upload_fingerprints


def device_of(path: Any) -> str:
    try:
        return _resolver.device_of(path)
    except Exception:  # Never fails an operation.
        return "unknown"


def device_of_entry(root: Any, logical_path: str) -> str:
    """The device of a Vault-relative path, joined lexically.

    No ``resolve()``: on a FUSE union every path component would cost a
    metadata round trip, and the device never authorizes a read.
    """
    try:
        from .vault_paths import safe_relative_path
        return device_of(os.path.join(os.fspath(root), *safe_relative_path(logical_path).parts))
    except Exception:
        return "unknown"


@contextmanager
def reading(path: Any, check: Callable[[], None] | None = None) -> Iterator[None]:
    """Hold a read slot of ``path``'s Source Device while its body is read."""
    with slots().reading(device_of(path), check):
        yield


def status() -> dict[str, Any]:
    """Installation-wide read scheduling state for ``GET /api/stats``."""
    priority = server_priority().status()
    from .throughput import METER
    return {
        "server_priority_enabled": priority["enabled"],
        "server_priority_throttled": priority["throttled"],
        "server_priority_throttled_since": priority["since"],
        # Of a device with no known type; each device carries its own (#766).
        "source_device_read_limit": slots().limit(),
        "source_devices": slots().snapshot(METER.device_rates()),
    }


def reset_for_tests(*, limit: int | None = None, threshold: float | None = None,
                    sys_root: str | None = None, mdstat: str | None = None) -> None:
    """Fresh registry; explicit values replace the configured ones."""
    global _slots, _priority, _resolver, _hot, _probe, _upload_fingerprints
    with _lock:
        _resolver, _hot, _upload_fingerprints = DeviceResolver(), HotFiles(), UploadFingerprints()
        _probe = RotationalProbe(sys_root=sys_root or SYS_ROOT, mdstat=mdstat or PROC_MDSTAT)
        _priority = ServerPriority(threshold) if threshold is not None else None
        _slots = (DeviceSlots(lambda: 1 if (_priority is not None and _priority.throttled) else limit)
                  if limit is not None else None)


def _after_fork() -> None:
    global _lock, _slots, _priority
    _lock, _slots, _priority = threading.Lock(), None, None


os.register_at_fork(after_in_child=_after_fork)
