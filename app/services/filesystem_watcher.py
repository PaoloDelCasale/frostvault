"""The filesystem watcher: one watcher per active Vault, kept registered (#795).

``watch_loop`` keeps the set of watchers aligned with the active Vaults.
``watch_vault`` is one watcher: it registers the native observer (a child
process, ADR-0053; ``watch_plan`` says around which gaps), hands every new
folder to a successor observer before applying the batch that named it, pairs
each registration with a scan, and registers again after a failure.

The catalog, the scans, the Problems and the worker's health belong to the
worker: what the watcher asks of it is ``WatcherHost``, and it imports nothing
of storage. ``app.storage.watcher_host`` is the production Adapter, whose
members resolve the storage names when used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Iterable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from watchfiles import Change

from ..config import settings
from . import protection_stop as protection_stop_service
from . import source_layout, vault_relocation, watch_plan
from . import worker_errors as worker_error_store
from .operation_policies import SOURCE_WATCHER_DENIED
from .problems import display_path
from .vault_paths import is_restore_temporary_name

# The watcher's own findings (#721), printed beside the traversal's: uvicorn's
# handler prints INFO in production.
_logger = logging.getLogger("uvicorn.error")
# The raw watcher batch is logged at DEBUG with at most this many paths.
DEBUG_BATCH_PATHS = 50
# Set while ``watch_loop`` runs in this process (#862).
_loop_running = threading.Event()


class WatcherHost(Protocol):
    """What the watcher asks of the worker whose Vaults it watches."""

    # A run that stayed up this long is healthy again: its restart pause resets.
    stable_seconds: float
    # Two scans for lost events are never closer than this.
    gap_rescan_min_seconds: float
    # How often the paths a scan could not observe are looked at again.
    recheck_seconds: float
    # A batch that fails this many times in a row is dropped.
    batch_max_failures: int
    # Failures in a row on the same vanished folder before the watcher counts as stopped.
    vanished_max_retries: int
    # The pause before registering again around a vanished folder.
    vanished_restart_seconds: float

    def restart_delay(self, streak: int) -> float:
        """The pause before a watcher that failed ``streak`` times in a row registers again."""

    def observe(self, *paths: Path, **options: Any) -> AsyncIterator[set[tuple[Change, str]]]:
        """A native observer of ``paths``, with ``watchfiles.awatch``'s options and yields."""

    def enabled_vaults(self) -> list[dict[str, Any]]:
        """The active Vaults, as rows with ``id``, ``source_root`` and their root identity."""

    def vault_inactive(self, vault_id: int) -> bool:
        """Whether the Vault exists and is not active (one catalog read)."""

    def record_source_health(
        self, vault_id: int, *, ok: bool, error: str, notify_problem: bool = True,
    ) -> None:
        """Record the health of the Vault's source component."""

    def unknown_directories(
        self, vault_id: int, root: Path, changes: set[tuple[Change, str]],
        handed_off: set[tuple[int, int, int]],
    ) -> set[tuple[Change, str]]:
        """Folders a directory event reveals and the catalog does not know (#721),
        as added directories; ``handed_off`` keeps one handoff per folder identity."""

    def apply_changes(
        self, vault: dict[str, Any], changes: set[tuple[Change, str]], *, wait: bool,
    ) -> int | None:
        """Apply one batch to the catalog; None when the Vault is busy and ``wait`` is False."""

    def reconcile(self, vault: dict[str, Any]) -> Awaitable[ScanOutcome]:
        """Run the scan a freshly registered watcher is paired with."""

    def resolve_error(self, vault_id: int) -> None:
        """Close the watcher's own failure of this Vault."""

    def report_error(self, exc: BaseException, vault_id: int) -> None:
        """Record an internal failure of this Vault's watcher."""

    def report_unreadable(self, vault_id: int, paths: Iterable[str]) -> None:
        """Open the unreadable-path Problem of each Vault-relative folder."""

    def note_last_error(self, vault_id: int, message: str) -> None:
        """Show ``message`` as the Vault's last error."""


class ScanOutcome(NamedTuple):
    # Seconds until the Vault must be scanned again; None when reconciled.
    retry_after: float | None = None
    # The scan was complete except for paths it named: re-check only those.
    recheck: bool = False


def watch_filter(_: Change, path: str) -> bool:
    return not is_restore_temporary_name(Path(path).name)


def added_directories(root: Path, changes: set[tuple[Change, str]]) -> set[tuple[Change, str]]:
    directories = set()
    for change, value in changes:
        entry = Path(value)
        if change != Change.added or is_restore_temporary_name(entry.name):
            continue
        try:
            entry.relative_to(root)
            if entry.is_dir() and not entry.is_symlink():
                directories.add((Change.added, value))
        except (ValueError, OSError):
            continue
    return directories


def log_batch(vault_id: int, changes: set[tuple[Change, str]]) -> None:
    """The raw native batch at DEBUG, to tell what a source reports (#721)."""
    if not changes or not _logger.isEnabledFor(logging.DEBUG):
        return
    shown = sorted((change.name, display_path(value)) for change, value in changes)
    more = len(shown) - DEBUG_BATCH_PATHS
    _logger.debug(
        "Vault %s: watcher batch of %d changes: %s%s",
        vault_id, len(shown),
        ", ".join(f"{name} {path}" for name, path in shown[:DEBUG_BATCH_PATHS]),
        f" (and {more} more)" if more > 0 else "",
    )


def _flattened_exceptions(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _flattened_exceptions(inner)
    else:
        yield exc


def is_undecodable_path_error(exc: BaseException) -> bool:
    """watchfiles raises this when an event or a directory is not UTF-8.

    The native observer cannot represent such a path, so it stops. That is a
    property of the filename, not an internal failure of the watcher.
    """
    return any("Unable to decode path" in str(inner) for inner in _flattened_exceptions(exc))


_DENIED_PATH = re.compile(r"IO error for operation on (.+?): Permission denied")


def denied_directories(exc: BaseException, root: Path) -> set[str]:
    """Folders a native or polling observer could not enroll, relative to root.

    Native registration raises PermissionError with notify's path list; polling
    reports an IO error naming one path. Keep the discovery even if permissions
    have already been repaired: a later readable walk cannot prove coverage.
    """
    found: set[str] = set()
    for inner in _flattened_exceptions(exc):
        paths = [match.group(1) for match in _DENIED_PATH.finditer(str(inner))]
        if isinstance(inner, PermissionError):
            # watchfiles 1.3: PermissionError has neither errno nor filename;
            # notify appends `about ["/path"]` to its message instead.
            _message, separator, encoded = str(inner).partition(" about ")
            if separator:
                try:
                    native_paths = json.loads(encoded)
                except (ValueError, TypeError):
                    native_paths = []
                if isinstance(native_paths, list):
                    paths.extend(value for value in native_paths if isinstance(value, str))
        for value in paths:
            path = Path(value)
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative != "." and path.is_dir():
                found.add(relative)
    return found


def vanished_directories(exc: BaseException, root: Path) -> set[str]:
    """Vault-relative paths that vanished while the observer registered them.

    watchfiles 1.3 raises FileNotFoundError with neither errno nor filename;
    notify appends `about ["/path"]` to its message instead.
    """
    found: set[str] = set()
    for inner in _flattened_exceptions(exc):
        if not isinstance(inner, FileNotFoundError):
            continue
        paths = [inner.filename] if isinstance(inner.filename, str) else []
        _message, separator, encoded = str(inner).partition(" about ")
        if separator:
            try:
                native_paths = json.loads(encoded)
            except (ValueError, TypeError):
                native_paths = []
            if isinstance(native_paths, list):
                paths.extend(value for value in native_paths if isinstance(value, str))
        for value in paths:
            try:
                relative = Path(value).relative_to(root).as_posix()
            except ValueError:
                continue
            if relative != ".":
                found.add(relative)
    return found


async def watch_vault(host: WatcherHost, vault: dict[str, Any]) -> None:
    """Watch one Vault until cancelled, restarting its observer when it stops."""
    try:
        await _run(host, vault)
    finally:
        watch_plan.forget_registration(int(vault["id"]), owner=asyncio.current_task())


async def _run(host: WatcherHost, vault: dict[str, Any]) -> None:
    # Some Source Volume backends do not reliably enroll new directories.
    # Install the successor BEFORE draining/closing the old recursive observer:
    # subtree catch-up alone cannot repair unrelated sibling events in a gap.
    pending_reconcile: set[tuple[Change, str]] = set()
    vault_id = int(vault["id"])
    loop = asyncio.get_running_loop()
    # A new watcher task starts from what a scan finds, not from a past root.
    watch_plan.forget(vault_id)
    # Every watcher run is paired with a scan once it is registered and healthy
    # (issues #496, #497): what happened before the watcher existed, or while it
    # was down, is invisible to it. None means no full scan is due.
    next_scan_at: float | None = 0.0
    # Due time of the cheap re-check of the paths the last scan could not observe.
    recheck_at: float | None = None
    failure_streak = 0
    last_scan_end = float("-inf")
    batch_failures = 0
    # What the registered observer was built around; a change re-registers it.
    registered_gaps = watch_plan.UnobservedPaths()
    registered_debt: dict[str, int] = {}

    # The paired scan runs beside the observer, never inside its loop (#690):
    # while it waits for its turn or walks the tree, events are still consumed
    # and applied whenever the Vault is free.
    scan_task: asyncio.Task[ScanOutcome] | None = None
    # Bumped each time a scan becomes due. A scan that was already running when
    # it was bumped may have walked past the gap that made it due.
    scan_due_mark = 0
    scan_started_mark = 0
    # Consecutive registrations that failed on the same vanished folder.
    vanished_streak: dict[str, int] = {}
    # Folders found by a one-level check and handed to a successor (#721).
    handed_off: set[tuple[int, int, int]] = set()

    def request_scan(at: float) -> None:
        nonlocal next_scan_at, scan_due_mark
        next_scan_at = at
        scan_due_mark += 1

    def schedule_gap_scan() -> None:
        request_scan(max(loop.time(), last_scan_end + host.gap_rescan_min_seconds))

    def start_reconcile() -> None:
        nonlocal scan_task, scan_started_mark
        if scan_task is None:
            scan_started_mark = scan_due_mark
            scan_task = asyncio.create_task(host.reconcile(vault))

    def collect_reconcile() -> None:
        nonlocal scan_task, next_scan_at, recheck_at, last_scan_end
        if scan_task is None or not scan_task.done():
            return
        finished, scan_task = scan_task, None
        outcome = finished.result()
        last_scan_end = loop.time()
        retry_at = None if outcome.retry_after is None else last_scan_end + outcome.retry_after
        if scan_due_mark != scan_started_mark and next_scan_at is not None:
            # A gap opened while this scan ran: it stays due, at the usual pace.
            gap_at = max(next_scan_at, last_scan_end + host.gap_rescan_min_seconds)
            next_scan_at = gap_at if retry_at is None else min(gap_at, retry_at)
        else:
            next_scan_at = retry_at
        recheck_at = last_scan_end + host.recheck_seconds if outcome.recheck else None

    def cancel_scan(_task: asyncio.Future[Any]) -> None:
        # A stopped watcher leaves no paired scan behind in the event loop.
        if scan_task is not None:
            scan_task.cancel()

    current = asyncio.current_task()
    if current is not None:
        current.add_done_callback(cancel_scan)
    while True:
        if vault_relocation.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        if protection_stop_service.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        if host.vault_inactive(vault["id"]):
            await asyncio.sleep(1)
            continue
        # Identity / mount gate first — never resolve() or walk the configured
        # tree while local operations are fail-closed (replacement, absent,
        # inaccessible, ambiguous, unsafe alias).
        access = await asyncio.to_thread(
            source_layout.vault_local_access, vault["source_root"]
        )
        if not access.local_operations_allowed:
            await asyncio.to_thread(
                host.record_source_health, vault_id,
                ok=False, error="Protection blocked: Source Volume unavailable",
            )
            schedule_gap_scan()
            await asyncio.sleep(5)
            continue
        try:
            root = Path(vault["source_root"]).resolve()
            if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
                raise PermissionError("Vault root is inaccessible")
        except OSError:
            await asyncio.to_thread(
                host.record_source_health, vault_id,
                ok=False, error="Protection blocked: Vault root inaccessible",
            )
            schedule_gap_scan()
            await asyncio.sleep(5)
            continue
        recovered = False
        applying = False
        started_at = loop.time()
        try:
            runtime = settings

            def new_observer():
                nonlocal registered_gaps, registered_debt
                gaps, registered_debt = watch_plan.registration_plan(vault_id)
                registered_gaps = gaps
                options = dict(
                    watch_filter=watch_filter,
                    debounce=runtime.filesystem_watch_debounce_ms,
                    force_polling=settings.filesystem_watch_force_polling,
                    poll_delay_ms=runtime.filesystem_watch_poll_ms,
                    # Native EACCES MUST surface (#565): ignoring it can yield
                    # a successful barrier with a new directory silently left
                    # unwatched. A later chmod/chown raises no topology event.
                    # Record the named gap and restart around it instead. Keep
                    # polling's existing snapshot/permission behavior unchanged.
                    ignore_permission_denied=settings.filesystem_watch_force_polling,
                    # awatch is lazy: its first yield (including a timeout)
                    # is the registration barrier, for inotify AND polling.
                    yield_on_timeout=True,
                    rust_timeout=1000,
                )
                if not gaps.directories:
                    return host.observe(root, recursive=True, **options)
                flat, recursive = watch_plan.watch_directories(root, gaps)
                observers = []
                if flat:
                    observers.append(host.observe(*flat, recursive=False, **options))
                if recursive:
                    observers.append(host.observe(*recursive, recursive=True, **options))
                return watch_plan.MergedWatch(observers)

            watcher = new_observer()
            successor = None
            rearm_required = False
            try:
                while True:
                    native_changes = set() if rearm_required else await anext(watcher)
                    collect_reconcile()
                    if not rearm_required:
                        watch_plan.record_registration(
                            vault_id, registered_gaps, owner=asyncio.current_task(), debt=registered_debt,
                        )
                    pending_reconcile |= native_changes
                    log_batch(vault_id, native_changes)
                    new_directories = await asyncio.to_thread(
                        added_directories, root, native_changes,
                    ) if native_changes else set()
                    if native_changes and not settings.filesystem_watch_force_polling:
                        # A folder reported only as a modified folder or parent
                        # (Unraid fuse.shfs, #721) is an added directory: the
                        # successor registers it before the batch is applied,
                        # and the batch walks it whole.
                        discovered = await asyncio.to_thread(
                            host.unknown_directories, vault_id, root, native_changes, handed_off,
                        )
                        pending_reconcile |= discovered
                        new_directories |= discovered
                    # PollWatcher recursively rediscovers topology every poll;
                    # it needs no re-registration. Keep its original snapshot
                    # alive: a timeout is NOT a completed polling-cycle fence,
                    # so replacing it could lose pre-successor sibling changes.
                    if (
                        (new_directories and not settings.filesystem_watch_force_polling)
                        or rearm_required
                    ):
                        old_gaps, old_debt = registered_gaps, registered_debt
                        successor = new_observer()
                        # Old observer is still live while the successor takes
                        # its recursive snapshot / registers native watches.
                        try:
                            first = await anext(successor)
                        except Exception as exc:
                            vanished = await asyncio.to_thread(
                                vanished_directories, exc, root,
                            )
                            if not vanished or any(
                                vanished_streak.get(path, 0) + 1 >= host.vanished_max_retries
                                for path in vanished
                            ):
                                raise
                            # A folder vanished while the successor registered
                            # it (#690). The old observer never stopped, so no
                            # event was lost: keep it, walk that path, and
                            # register again. Not a failure of the watcher.
                            for path in vanished:
                                vanished_streak[path] = vanished_streak.get(path, 0) + 1
                            failed, successor = successor, None
                            await failed.aclose()
                            registered_gaps, registered_debt = old_gaps, old_debt
                            pending_reconcile |= {(Change.added, str(root / path)) for path in vanished}
                            rearm_required = True
                            await asyncio.sleep(host.vanished_restart_seconds)
                            continue
                        vanished_streak.clear()
                        settled = watch_plan.record_registration(
                            vault_id, registered_gaps, owner=asyncio.current_task(), debt=registered_debt,
                        )
                        if (old_gaps.directories | settled) - registered_gaps.directories:
                            # Coverage expanded: files may predate the successor's
                            # snapshot. Pair this handoff with a scan, just like
                            # a healthy watcher start (ADR 0027), before success.
                            request_scan(loop.time())
                        pending_reconcile |= first
                        tail = await anext(watcher)
                        pending_reconcile |= tail
                        old, watcher = watcher, successor
                        successor = None
                        await old.aclose()
                        # Every directory addition DURING registration needs
                        # another handoff, even at a pathname in the original
                        # batch: that directory may have been replaced after
                        # the successor registered its old inode. A duplicate
                        # notification costs one extra handoff; deduplicating by
                        # pathname can silently leave the new inode unwatched.
                        # No full-root walk, and at most two observers.
                        additions = await asyncio.to_thread(
                            added_directories, root, first | tail,
                        )
                        log_batch(vault_id, first | tail)
                        discovered = await asyncio.to_thread(
                            host.unknown_directories, vault_id, root, first | tail, handed_off,
                        ) if not settings.filesystem_watch_force_polling else set()
                        pending_reconcile |= discovered
                        rearm_required = bool(additions or discovered)
                    if pending_reconcile and not rearm_required:
                        # Reconcile only once every added subtree is covered by
                        # a registered successor, including additions made in
                        # the preceding handoff itself.
                        # A busy Vault (its scan walking, a Job holding it)
                        # keeps the batch: events go on being consumed and
                        # the batch is applied once the Vault is free (#690).
                        applying = True
                        applied = await asyncio.to_thread(
                            host.apply_changes, vault, pending_reconcile, wait=False,
                        )
                        applying = False
                        if applied is not None:
                            batch_failures = 0
                            pending_reconcile = set()
                    if not recovered and not pending_reconcile and not rearm_required:
                        # First fully healthy cycle of this run: registration
                        # barrier passed and every pending change applied.
                        await asyncio.to_thread(
                            host.resolve_error, vault_id,
                        )
                        recovered = True
                    if recheck_at is None and watch_plan.unobserved(vault_id):
                        # A batch or a failed observer found a gap outside a
                        # full scan: its repair raises no event either.
                        recheck_at = loop.time() + host.recheck_seconds
                    if (
                        recovered and not pending_reconcile and not rearm_required
                        and scan_task is None
                        and next_scan_at is not None and loop.time() >= next_scan_at
                    ):
                        recheck_at = None
                        start_reconcile()
                    elif (
                        recovered and not pending_reconcile and not rearm_required
                        and scan_task is None
                        and recheck_at is not None and loop.time() >= recheck_at
                    ):
                        known = watch_plan.unobserved(vault_id)
                        repaired = not await asyncio.to_thread(
                            watch_plan.still_unobserved, root, known,
                        )
                        if repaired:
                            start_reconcile()
                        else:
                            recheck_at = loop.time() + host.recheck_seconds
                    if (
                        recovered and not rearm_required
                        and (watch_plan.unobserved(vault_id).directories != registered_gaps.directories
                             or watch_plan.coverage_debt(vault_id))
                    ):
                        # A permission was repaired, a name renamed, or a folder
                        # turned out unreadable: register the tree again.
                        rearm_required = True
            finally:
                if successor is not None:
                    await successor.aclose()
                await watcher.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = loop.time()
            collect_reconcile()
            # A folder that vanished while the observer registered it is the
            # churn of a live source, not a broken watcher (#690): register
            # again soon, quietly, and walk that path. The events around the
            # stopped observer are lost all the same: the gap scan stays.
            vanished = await asyncio.to_thread(vanished_directories, exc, root)
            if now - started_at >= host.stable_seconds:
                # The observer stayed up: whatever vanished before is history.
                vanished_streak.clear()
            quiet_vanish = bool(vanished) and all(
                vanished_streak.get(path, 0) + 1 < host.vanished_max_retries for path in vanished
            )
            for path in vanished:
                vanished_streak[path] = vanished_streak.get(path, 0) + 1
            if quiet_vanish:
                pending_reconcile |= {(Change.added, str(root / path)) for path in vanished}
            else:
                # Quick repeated failures grow the pause. The paired scan runs
                # beside the observer, so its time is not counted either way.
                uptime = now - started_at
                failure_streak = 1 if uptime >= host.stable_seconds else failure_streak + 1
            schedule_gap_scan()
            if applying and pending_reconcile:
                batch_failures += 1
                if batch_failures >= host.batch_max_failures:
                    worker_error_store.log_worker_error("source_watcher_batch_dropped", exc, vault_id)
                    pending_reconcile = set()
                    batch_failures = 0
            denied = await asyncio.to_thread(denied_directories, exc, root)
            if denied:
                watch_plan.note_unreadable_directories(vault_id, denied)
                # Registration can fail before any batch/scan sees the denial,
                # and the folder can already be readable by the next walk.
                # Publish its path now, independently of the retry scan.
                host.report_unreadable(vault_id, denied)
            if quiet_vanish:
                logging.getLogger(__name__).info(
                    "Source watcher of Vault %s registers again: %s vanished while it was "
                    "being registered", vault_id, ", ".join(sorted(vanished)),
                )
            elif is_undecodable_path_error(exc):
                # A filename that is not UTF-8 stopped the native observer and the
                # events around it are lost: not an internal error. The Vault is
                # not confirmed until a scan has looked again; that scan names the
                # path, and the restart registers the tree around a directory
                # with such a name (#494).
                worker_error_store.log_worker_error("source_watcher", exc, vault_id)
                await asyncio.to_thread(
                    host.record_source_health, vault_id,
                    ok=False, notify_problem=False,
                    error="Source watcher stopped on a file name that is not valid UTF-8",
                )
            elif denied:
                # Native registration or the polling backend could not enter
                # a folder: a path problem, not an internal error. The restart
                # registers around it and the cheap retry repairs coverage.
                # One line, no traceback: the file_unreadable Problem names
                # the path and real-time detection goes on elsewhere (#681).
                logging.getLogger(__name__).warning(
                    "Source watcher of Vault %s cannot enter %s; the rest of the Vault stays "
                    "watched in real time (see the unreadable-path Problem)",
                    vault_id, ", ".join(sorted(denied)),
                )
                await asyncio.to_thread(
                    host.record_source_health, vault_id,
                    ok=False, notify_problem=False,
                    error=SOURCE_WATCHER_DENIED,
                )
            else:
                host.note_last_error(vault_id, f"Source watcher: {exc}")
                if isinstance(exc, OSError):
                    await asyncio.to_thread(
                        host.record_source_health, vault_id,
                        ok=False, error="Protection blocked: Source watcher stopped on an unreadable path",
                    )
                else:
                    await asyncio.to_thread(host.report_error, exc, vault_id)
            if (
                not recovered and not denied and not quiet_vanish
                and next_scan_at is not None and loop.time() >= next_scan_at
            ):
                # Unknown registration failures still need a scan to name the
                # gap. A named permission gap already has a problem and plan:
                # wait for registration around it before scanning. Otherwise a
                # repair before this walk could clear source health without any
                # registered observer, and consume the start's paired scan.
                start_reconcile()
            await asyncio.sleep(
                host.vanished_restart_seconds if quiet_vanish
                else host.restart_delay(failure_streak)
            )


def root_signature(vault: dict[str, Any]) -> tuple[Any, ...]:
    # Reconnect may accept a different inode/mount at the SAME pathname. A
    # pathname-only signature would leave a watcher attached to the old tree.
    return (vault["source_root"], vault.get("root_identity"),
            vault.get("reconnect_volume_fingerprint"))


def start_blocker(vault: dict[str, Any]) -> str | None:
    """Why ``watch_loop`` starts no watcher for this active Vault, or None.

    ``"suspended"``: its local work is suspended (relocation);
    ``"unavailable"``: its Source Volume refuses local operations.
    """
    if vault_relocation.local_work_suspended(vault):
        return "suspended"
    if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
        return "unavailable"
    return None


def pairs_startup_scan(vault: dict[str, Any]) -> bool:
    """Whether a watcher of this process scans this active Vault once registered (#862).

    True while ``watch_loop`` runs and would start a watcher for the Vault:
    every watcher run is paired with a scan, so the scheduler's startup scan
    of the Vault would walk it a second time.
    """
    return _loop_running.is_set() and start_blocker(vault) is None


async def watch_loop(host: WatcherHost) -> None:
    """Keep one recursive filesystem watcher aligned with each enabled vault."""
    watchers: dict[int, tuple[tuple[Any, ...], asyncio.Task[None]]] = {}
    _loop_running.set()
    try:
        while True:
            await asyncio.to_thread(source_layout.verify_mounts_once)
            vaults = await asyncio.to_thread(host.enabled_vaults)
            desired = {int(vault["id"]): vault for vault in vaults}
            for vault_id, (signature, task) in list(watchers.items()):
                vault = desired.get(vault_id)
                if vault is None or root_signature(vault) != signature or task.done():
                    task.cancel()
                    watchers.pop(vault_id)
            for vault_id, vault in desired.items():
                blocker = start_blocker(vault)
                if blocker is not None:
                    if blocker == "unavailable":
                        await asyncio.to_thread(
                            host.record_source_health, vault_id,
                            ok=False, error="Protection blocked: Source Volume unavailable",
                        )
                    existing = watchers.get(vault_id)
                    if existing is not None:
                        existing[1].cancel()
                        watchers.pop(vault_id, None)
                    continue
                if vault_id not in watchers:
                    watchers[vault_id] = (
                        root_signature(vault),
                        asyncio.create_task(watch_vault(host, vault)),
                    )
            await asyncio.sleep(10)
    finally:
        _loop_running.clear()
        tasks = [task for _, task in watchers.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
