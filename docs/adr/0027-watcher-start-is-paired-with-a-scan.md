# A healthy watcher start is paired with a scan

Issues #494, #495, #496 and #497 show one fault with four faces: the filesystem
watcher is the only thing that notices change between the six-hour scans, so
whatever stops it, or happens before it exists, stays invisible and keeps the
Vault `unconfirmed` and its notifications open until a scan happens to run.

## Decision

1. **Every healthy watcher start runs one full scan.** After the native observer
   has passed its registration barrier and applied its pending changes, the Vault
   is scanned (`scan_vault`, the same scan as the periodic one). Registration comes
   first, so events during the scan are seen by the watcher and older ones by the
   scan. This covers a new Vault (#497), a restart, and the return from any
   failure or from an unavailable Vault folder (#496). The scan clears the
   `source` health error and, through the existing reconciliation, the
   `file_unreadable` problems whose cause is gone.
2. **An incomplete scan is repeated every 15 minutes.** An unreadable folder is
   not watched, so fixing its permissions raises no event. A scan that reports
   unreadable paths (or fails) is retried at that interval until it completes. A
   scan that finds another scan running is retried after ten seconds.
3. **An unreadable subfolder does not stop the watcher** (`ignore_permission_denied`).
   The scan names the folder as `file_unreadable`; the rest of the Vault is watched
   and uploaded as usual (#495). The `source` health message of a failed watcher no
   longer claims that the root is unreadable.
4. **A name that is not valid UTF-8 is its own problem, `file_name_unsupported`.**
   It cannot be stored in the catalog nor become a cloud key. Scans skip it (and
   everything beneath it) and report the path in escaped form; it does not make
   the scan incomplete, because no catalog row can exist at or below such a path,
   so nothing else becomes unconfirmed (#494). The native observer stops on an
   event for such a path (`Unable to decode path`): that is not an internal error.
   The watcher restarts and the following scan reconciles what the stopped
   observer lost. A complete walk of the whole Vault closes the unsupported-name
   problems it no longer sees.
5. **Restarts back off.** The pause after a watcher failure grows from 5 s to
   5 min while failures keep coming, and resets after a run of one minute. A watcher
   that cannot register at all still reconciles its Vault at most every five minutes.
6. **Recovered workers close their internal error.** `auto_upload_queue` and
   `cloud_preflight` close their `internal_error` problem on the first successful
   pass (once, not on every pass), as `source_watcher` already did (#496).

## Consequences

- Each application start scans every enabled Vault once (local walk, cloud
  listing, Storage Plan synchronization). This is the price of never leaving files
  unseen for up to six hours; the periodic scan is unchanged.
- With `FILESYSTEM_WATCH_FORCE_POLLING=true` the polling backend still fails on an
  unreadable subfolder (it cannot skip it). The watcher stops, restarts with
  backoff and the Vault is reconciled by scans; the path is still named.
- A scan with an unreadable path still leaves the Vault `unconfirmed` while it
  lasts (an incomplete walk cannot prove absence). That rule is unchanged;
  only unsupported names are exempt, because they hide nothing from the catalog.
