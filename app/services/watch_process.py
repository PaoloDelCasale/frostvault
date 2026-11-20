"""``awatch`` with the native observer in a child process (ADR-0053).

watchfiles registers inotify watches in its Rust constructor without
releasing the GIL. With thousands of folders that stalls every thread of the
server, the event loop serving /health included, for minutes (#777). This
module keeps the signature and the yields of ``watchfiles.awatch``, but the
``RustNotify`` lives in ``watch_child.py``, a small process with its own GIL.

Semantics kept from ``awatch``: lazy start at the first iteration, one
``RustNotify.watch`` call per iteration (events accumulate in between), the
same change sets after ``watch_filter``, empty sets on timeout when
``yield_on_timeout`` is set, and the observer's exceptions raised by the
iteration with their type and message (the watcher reads paths from them).
Closing the generator kills the child; so does the server's death.
"""
from __future__ import annotations

import asyncio
import atexit
import builtins
import importlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from watchfiles import Change, DefaultFilter
from watchfiles.main import (
    _calc_async_timeout,
    _default_debug,
    _default_force_polling,
    _default_ignore_permission_denied,
    _default_poll_delay_ms,
)

CHILD_SCRIPT = Path(__file__).with_name("watch_child.py")
# One reply line is at most one chunk of changes; a path is at most PATH_MAX.
_LINE_LIMIT = 64 * 1024 * 1024

_live: set[asyncio.subprocess.Process] = set()


class ObserverProcessExited(RuntimeError):
    """The observer process ended without reporting an error."""


def _rebuild_error(payload: dict[str, Any]) -> Exception:
    """The child's exception with its type, message, errno and filename."""
    module, name = str(payload.get("module")), str(payload.get("name"))
    message = str(payload.get("message", ""))
    cls: Any = None
    if module == "builtins":
        cls = getattr(builtins, name, None)
    elif module == "watchfiles" or module.startswith("watchfiles."):
        try:
            cls = getattr(importlib.import_module(module), name, None)
        except ImportError:
            cls = None
    if not (isinstance(cls, type) and issubclass(cls, Exception)):
        return RuntimeError(f"{name}: {message}")
    try:
        if issubclass(cls, OSError) and payload.get("errno") is not None:
            parts = [payload["errno"], payload.get("strerror")]
            if payload.get("filename") is not None:
                parts.append(payload["filename"])
            return cls(*parts)
        return cls(*payload.get("args", []))
    except Exception:
        return RuntimeError(f"{name}: {message}")


async def _read_reply(process: asyncio.subprocess.Process) -> dict[str, Any]:
    assert process.stdout is not None
    line = await process.stdout.readline()
    if not line:
        code = await process.wait()
        raise ObserverProcessExited(f"Filesystem observer process exited with code {code}")
    return json.loads(line)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            # Registration holds the child's GIL and there is nothing to flush:
            # SIGKILL ends it at once. The kernel drops its inotify watches.
            process.kill()
        except ProcessLookupError:
            pass
    if process.stdin is not None:
        process.stdin.close()
    try:
        # The loop's child watcher reaps it even if this wait is cancelled.
        await process.wait()
    finally:
        _live.discard(process)
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()


async def awatch(
    *paths: Path | str,
    watch_filter: Callable[[Change, str], bool] | None = DefaultFilter(),
    debounce: int = 1_600,
    step: int = 50,
    rust_timeout: int | None = None,
    yield_on_timeout: bool = False,
    debug: bool | None = None,
    force_polling: bool | None = None,
    poll_delay_ms: int = 300,
    recursive: bool = True,
    ignore_permission_denied: bool | None = None,
) -> AsyncGenerator[set[tuple[Change, str]], None]:
    config = {
        "parent_pid": os.getpid(),
        "paths": [str(path) for path in paths],
        "debug": _default_debug(debug),
        "force_polling": _default_force_polling(force_polling),
        "poll_delay_ms": _default_poll_delay_ms(poll_delay_ms),
        "recursive": bool(recursive),
        "ignore_permission_denied": _default_ignore_permission_denied(ignore_permission_denied),
        "debounce": int(debounce),
        "step": int(step),
        "timeout": _calc_async_timeout(rust_timeout),
    }
    # Started from the event loop thread: PR_SET_PDEATHSIG follows the thread
    # that forked the child, and this one lives as long as the server.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-P", str(CHILD_SCRIPT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        limit=_LINE_LIMIT,
    )
    _live.add(process)
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(config).encode("ascii") + b"\n")
        while True:
            process.stdin.write(b"watch\n")
            await process.stdin.drain()
            reply = await _read_reply(process)
            if "error" in reply:
                raise _rebuild_error(reply["error"])
            result = reply.get("result")
            if result == "timeout":
                if yield_on_timeout:
                    yield set()
                continue
            if result == "stop":
                return
            if result == "signal":
                raise RuntimeError("watch thread unexpectedly received a signal")
            raw = list(reply.get("changes", []))
            while reply.get("more"):
                reply = await _read_reply(process)
                if "error" in reply:
                    raise _rebuild_error(reply["error"])
                raw.extend(reply.get("changes", []))
            changes = {(Change(int(change)), str(path)) for change, path in raw}
            if watch_filter:
                changes = {item for item in changes if watch_filter(item[0], item[1])}
            if changes:
                yield changes
    finally:
        await _stop(process)


def live_observer_count() -> int:
    return sum(1 for process in _live if process.returncode is None)


@atexit.register
def _kill_leftovers() -> None:
    # A generator never closed (loop gone): no observer outlives the server.
    for process in list(_live):
        if process.returncode is None:
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
