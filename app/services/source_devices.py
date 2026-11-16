"""Reads of Local Copies per Source Device (#713, ADR-0046).

The Source Device of a Local Copy is the disk its bytes are read from. On an
Unraid user share (FUSE ``shfs``) it is the array disk named by the extended
attribute ``system.LOCATION`` (``disk1``, ``disk2``, ``cache``); elsewhere it
is the ``st_dev`` of the file. A guess is cached per parent folder, because a
folder's files mostly sit on one disk. A wrong guess only costs scheduling
efficiency: the device never decides what is read or published, and looking
it up never fails an operation.

One process-wide registry bounds the reads in flight on each device,
``SOURCE_DEVICE_READ_CONCURRENCY`` at a time, in addition to the global limits
of scan verification and uploads, and shared by both. A slot is held only
while the body of a Local Copy is read. Waiting for one runs the caller's
checkpoint every :data:`RECHECK_SECONDS`, so cancellation, shutdown and claim
loss still win while queued.

The optional server priority (``SERVER_PRIORITY_IOWAIT_PERCENT``) samples the
host's iowait from ``/proc/stat``. Above the threshold for
:data:`THROTTLE_AFTER_SECONDS` FrostVault slows down: one read per device and
no new upload Jobs; it recovers after :data:`RECOVER_AFTER_SECONDS` below.
"""
from __future__ import annotations

import logging
import os
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


# -- Per-device read slots --------------------------------------------------

class DeviceSlots:
    """Reads in flight per device; one shared instance per process."""

    def __init__(self, limit: int | Callable[[], int]):
        self._limit = limit if callable(limit) else (lambda value=int(limit): value)
        self._condition = threading.Condition()
        self._reading: Counter[str] = Counter()
        self._waiting: Counter[str] = Counter()

    def limit(self) -> int:
        try:
            return max(1, int(self._limit()))
        except Exception:  # A broken setting never stops a read.
            return 1

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
                    if self._reading[device] < self.limit():
                        self._reading[device] += 1
                        acquired = True
                        break
                    self._condition.wait(RECHECK_SECONDS)
        finally:
            with self._condition:
                self._waiting[device] -= 1
                if not self._waiting[device]:
                    del self._waiting[device]
        try:
            yield
        finally:
            with self._condition:
                if acquired:
                    self._reading[device] -= 1
                    if not self._reading[device]:
                        del self._reading[device]
                self._condition.notify_all()

    def snapshot(self) -> list[dict[str, Any]]:
        """Bounded per-device counts for the runtime status (observability)."""
        with self._condition:
            devices = sorted(set(self._reading) | set(self._waiting))
            return [{"device": device.split(":", 1)[-1], "reading": self._reading[device],
                     "waiting": self._waiting[device]} for device in devices[:32]]


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
    limit = slots.limit()
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
                if device is None or slots.load(device) + picked[device] < limit:
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
                         busy: Callable[[str], int], limit: Callable[[], int],
                         lookahead: int = 8192, fill: int = 256) -> Iterator[tuple[str, Any] | None]:
    """Feed scan reads round-robin across devices (#713).

    Keeps up to ``lookahead`` items grouped by device (looking up at most
    ``fill`` more per step, so the first read starts at once) and yields
    ``(device, item)`` from the device after the last one served whose
    ``busy(device)`` (reads this scan has submitted and not finished) is below
    ``limit()``, so a busy disk never starves the others. Yields None when
    every buffered device is at its limit: the caller waits for a read to
    finish, then resumes the generator.
    """
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
        cap = limit()
        device = next((name for name in devices if busy(name) < cap), None)
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
_slots: DeviceSlots | None = None
_priority: ServerPriority | None = None


def _settings() -> Any:
    from ..config import settings
    return settings


def configured_limit() -> int:
    return max(1, min(16, int(getattr(_settings(), "source_device_read_concurrency", 3) or 3)))


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


def _current_limit() -> int:
    return 1 if throttled() else configured_limit()


def slots() -> DeviceSlots:
    global _slots
    with _lock:
        if _slots is None:
            _slots = DeviceSlots(_current_limit)
        return _slots


def _wake() -> None:
    with _lock:
        current = _slots
    if current is not None:
        current.wake()


def hot_files() -> HotFiles:
    return _hot


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
    return {
        "server_priority_enabled": priority["enabled"],
        "server_priority_throttled": priority["throttled"],
        "server_priority_throttled_since": priority["since"],
        "source_device_read_limit": slots().limit(),
        "source_devices": slots().snapshot(),
    }


def reset_for_tests(*, limit: int | None = None, threshold: float | None = None) -> None:
    """Fresh registry; explicit values replace the configured ones."""
    global _slots, _priority, _resolver, _hot
    with _lock:
        _resolver, _hot = DeviceResolver(), HotFiles()
        _priority = ServerPriority(threshold) if threshold is not None else None
        _slots = (DeviceSlots(lambda: 1 if (_priority is not None and _priority.throttled) else limit)
                  if limit is not None else None)


def _after_fork() -> None:
    global _lock, _slots, _priority
    _lock, _slots, _priority = threading.Lock(), None, None


os.register_at_fork(after_in_child=_after_fork)
