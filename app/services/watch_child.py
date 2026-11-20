"""Filesystem observer child process (ADR-0053).

Run by path (``python -P watch_child.py``), never imported by the app: it
imports only watchfiles and the standard library. Registering native watches
holds this process's GIL, not the server's.

Protocol, one JSON object per line:

- stdin, first line: the observer configuration.
- stdin, then: one line per request for the next batch. The child calls
  ``RustNotify.watch`` once per request, so events accumulate in notify
  between requests exactly as they do between two ``awatch`` iterations.
- stdout: per request ``{"result": "timeout" | "stop" | "signal"}``, or the
  batch as ``{"changes": [[change, path], ...], "more": bool}`` chunks, or
  ``{"error": {...}}`` followed by exit.

EOF on stdin (the server closed the pipe or died) ends the child; a parent
death during registration kills it through PR_SET_PDEATHSIG.
"""
import json
import os
import signal
import sys

CHUNK = 2000
PR_SET_PDEATHSIG = 1


def _die_with_parent(parent_pid: int) -> None:
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except (OSError, AttributeError):
        pass
    if os.getppid() != parent_pid:
        # The parent died before the death signal was armed.
        os._exit(0)


def _plain(value):
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def _error(exc: BaseException) -> dict:
    payload = {
        "module": type(exc).__module__,
        "name": type(exc).__qualname__,
        "args": [_plain(value) for value in exc.args],
        "message": str(exc),
    }
    if isinstance(exc, OSError):
        payload["errno"] = exc.errno
        payload["strerror"] = _plain(exc.strerror)
        payload["filename"] = exc.filename if isinstance(exc.filename, str) else None
    return {"error": payload}


def main() -> None:
    # The server owns this process: Ctrl+C on a terminal stops the server,
    # which then stops the observer.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    requests = sys.stdin.buffer
    replies = sys.stdout.buffer

    def send(message: dict) -> None:
        replies.write(json.dumps(message).encode("ascii") + b"\n")
        replies.flush()

    line = requests.readline()
    if not line:
        return
    config = json.loads(line)
    _die_with_parent(int(config["parent_pid"]))
    try:
        from watchfiles._rust_notify import RustNotify

        watcher = RustNotify(
            config["paths"], config["debug"], config["force_polling"],
            config["poll_delay_ms"], config["recursive"], config["ignore_permission_denied"],
        )
    except Exception as exc:
        send(_error(exc))
        return
    with watcher:
        for _request in requests:
            try:
                raw = watcher.watch(config["debounce"], config["step"], config["timeout"], None)
            except Exception as exc:
                send(_error(exc))
                return
            if isinstance(raw, str):
                send({"result": raw})
                continue
            changes = [[int(change), path] for change, path in raw]
            for start in range(0, max(len(changes), 1), CHUNK):
                send({
                    "changes": changes[start:start + CHUNK],
                    "more": start + CHUNK < len(changes),
                })


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        # The server went away: nothing left to report to.
        pass
    finally:
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
