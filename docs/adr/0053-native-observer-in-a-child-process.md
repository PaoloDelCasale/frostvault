# The native filesystem observer runs in a child process

Status: accepted by the Owner on 2026-11-20 (follow-up of issue #777, PR #782).

## Context

After a container restart the server answered nothing, not even `/ready`, for
about 184 s after `Application startup complete`. The cause is the watcher's
registration. `watchfiles.awatch(..., recursive=True)` builds a `RustNotify` at
its first iteration, and that Rust constructor walks the tree and adds one
inotify watch per folder **without releasing the GIL**. In production the
process held 4,763 + 10,411 watches across two Vaults. For the whole walk every
other Python thread stood still: the event loop serving the API and `/health`,
the workers, the Job threads. Moving the call to another thread changes
nothing, because the GIL stays taken.

The same stall comes back every time an observer is created again: the
successor observer of a new folder (#721), a rearm around a repaired
permission, and a restart after a failure.

Measured on the production host, a synthetic tree of 15,151 folders on
`fuse.shfs` (`/mnt/user/...`): registration takes 13–15 s, and the event loop
stalls for all of it (longest gap 12.8–14.0 s). On the NVMe pool directly the
same tree takes 1.2 s with a 0.2 s stall.

## Decision

- **The `RustNotify` lives in a small child process**
  (`app/services/watch_child.py`, imports only watchfiles and the standard
  library, about 24 MB resident). Its registration holds the child's GIL, not
  the server's. `app/services/watch_process.awatch` replaces
  `watchfiles.awatch` with the same signature and yields; the watcher code
  (`_run_vault_filesystem_watch`, `MergedWatch`, flat and recursive
  observers, successors) is unchanged.
- **One `watch()` call per iteration.** The server writes one request line per
  iteration and the child answers with one `RustNotify.watch` result.
  Between iterations events accumulate in notify exactly as they did in-process.
  The registration barrier (the first yield with `yield_on_timeout`), the
  handoff that drains the old observer after the successor registered, the
  debounce, `watch_filter` (applied in the server), `force_polling`,
  `ignore_permission_denied` and `rust_timeout` keep their meaning.
- **Errors cross the pipe with their type and message.** `PermissionError`,
  `FileNotFoundError` with notify's `about ["/path"]` list, the undecodable
  path error and watchfiles' own `WatchfilesRustInternalError` are rebuilt in
  the server, so the denied/vanished folder handling and the restart backoff
  work as before. A child that ends without reporting raises a `RuntimeError`,
  an ordinary watcher failure.
- **Lifecycle.** The child starts at the first iteration, from the event loop
  thread, as the server's own user: no extra privilege. Closing or cancelling
  the observer kills it (SIGKILL: nothing to flush, the kernel drops its
  watches) and the loop reaps it; Vault stop, deletion, root change and server
  shutdown all close observers through the existing code. If the server dies,
  the child gets SIGKILL from `PR_SET_PDEATHSIG` (needed while it registers and
  cannot read its pipe) or ends on EOF of its request pipe.
- **A plain `asyncio` subprocess, not `multiprocessing`.** The child is run by
  path with `python -P`: no pickling, no re-import of the server's `__main__`,
  no fork of a process with threads and open database connections, and the
  pipe is read by the event loop without a thread.

## Alternatives rejected

- **Batched non-recursive registration** (FrostVault enumerates folders and
  registers them flat). About 6× shorter in the #777 measurements, but still
  blocking in proportion to the tree, and it changes how subfolders created
  later are enrolled, the semantics #565 and #721 depend on.
- **Only a longer healthcheck `start_period`.** Docker stops restarting the
  container, but the app stays frozen just as long, and every successor
  observer still freezes it.
- **`PollWatcher`.** No registration stall, but it rescans the whole tree on
  every poll: far more I/O on a 137k-file Vault and slower detection.

## Consequences

- One extra process per observer (two per Vault during a handoff, about 24 MB
  each). Registration of several Vaults now runs in parallel.
- Each iteration costs a pipe round trip; with `rust_timeout=1000` that is
  negligible.
- `watch_process` imports watchfiles' private `_default_*` helpers to keep
  `awatch`'s defaults; a watchfiles upgrade must keep them or adapt the module
  (the pin in `requirements.txt` and `tests/test_watch_process.py` catch it).
