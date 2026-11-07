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
2. **An incomplete scan re-checks only what it named.** A folder that cannot be
   entered is not watched, so repairing its permissions raises no event. When a
   scan reports unreadable paths or unsupported names, FrostVault looks at those
   paths alone once a minute (a directory listing or a one-byte read each) and runs
   the full scan again as soon as one of them was repaired, renamed or removed. A
   path that stays broken therefore costs no repeated walk and no S3 listing; the
   six-hour scan is the only periodic one. A scan that fails, or whose S3 listing
   failed, is repeated after 15 minutes. A scan that finds another scan running is
   retried after ten seconds.
3. **The watcher registers the readable part of the tree around the gaps.** The
   native observer cannot register a directory the process may not enter, nor one
   whose name is not valid UTF-8 (`Unable to decode path`, before the first
   event). `ignore_permission_denied` is not enough for the first case: a
   recursive registration stops at the first denied directory, so which siblings
   stay watched depends on the order the filesystem lists names, and nothing
   tells the Owner. The scan therefore records the unobservable directories
   (`app/services/watch_plan.py`); the watcher watches every ancestor of one
   without recursion and every other folder next to those ancestors recursively,
   through one merged iterator with the same registration barrier. A scan, or a
   batch that scans a new folder, that changes the set registers the tree again,
   so a repaired folder is watched as a whole once the scan has seen it, and a
   folder created unreadable during the watch does not leave its siblings behind.
   The first registration after an application start knows nothing yet: if it
   fails on an undecodable directory, the scan that follows the failure names it
   and the next start registers around it. With `FILESYSTEM_WATCH_FORCE_POLLING`
   the same plan applies, since the plan never lists a folder the poller would
   fail on.
4. **A name that is not valid UTF-8 is its own problem, `file_name_unsupported`,
   and the Vault stays unconfirmed while it exists.** It cannot be stored in the
   catalog nor become a cloud key, so scans skip it with everything beneath it and
   report it in escaped form. But a name cannot be told apart from the one it
   replaced: when another client renames `Foto` to `Fot\xf2`, or a remount
   changes the charset, the rows under `Foto` would look removed and become
   "missing", cloud-only and deletable from the cloud. A name that cannot be
   catalogued therefore makes the walk incomplete, exactly like an unreadable
   path: no Local Copy becomes missing, no rename is recognized, the `source`
   health reports the scan as incomplete, and `observation_is_unconfirmed` blocks
   local release and cloud deletion for the whole Vault. The scope is the Vault,
   not the subtree: bytes cannot be mapped to the old text path, and a
   root-level name would cover everything anyway. Uploads of the other files are
   not blocked. The watcher records the same `source` error as soon as the
   observer stops on such a name, before the scan has run. A complete walk of the
   whole Vault closes the unsupported-name problems it no longer sees; the
   `file_unreadable` problems are closed one by one, except those below a folder
   the walk could not enter. Consequence for #503 (`_upload_source_confirmed_removed`,
   not on this branch): a file renamed to an unsupported name must never read as
   "removed" from a missing row alone. It does not, because no row goes missing
   while the walk is incomplete; the check must keep requiring a complete walk.
5. **Restarts back off, and scans after a stop are rate-limited.** The pause after
   a watcher failure grows from 5 s to 5 min and resets after a run that stayed up
   for a minute not counting the time spent scanning, so a long scan cannot hide a
   watcher that dies at once. Every stop loses events and is followed by a scan,
   but two such scans are at least five minutes apart: a burst of stops (a client
   writing many undecodable names) is one scan, not a scan per restart. A batch
   of changes that fails to apply three times in a row is dropped, as the next
   scan covers it; otherwise it would fail every cycle and no later event of the
   Vault would be applied. A folder that appears unreadable during a watch is a
   `file_unreadable` path of its own, not a replaced tree.
6. **Recovered workers close their internal error.** `auto_upload_queue` and
   `cloud_preflight` close their `internal_error` problem on the first successful
   pass (once, not on every pass), as `source_watcher` already did (#496).
7. **Paired scans take turns, and S3 recovery scans the Vaults that failed on S3.**
   The scans started by watchers run on one dedicated thread, so a restart of
   every watcher does not walk every Vault at once. A scan whose S3 listing failed
   is not reconciled. When the S3 readiness gate goes from blocked to ready (a
   corrected key, bucket or permission) the Vaults whose last cloud observation
   failed are scanned, without waiting for the periodic scan or a restart.

## Consequences

- Each application start scans every enabled Vault once (local walk, cloud
  listing, Storage Plan synchronization), one Vault at a time. This is the price
  of never leaving files unseen for up to six hours; the periodic scan is unchanged.
- While a path is unreadable or has an unsupported name, the Vault is
  `unconfirmed`: it is still uploaded, but no local space is freed and nothing is
  deleted from the cloud. The notifications say so. That rule is the one that
  already held for unreadable paths and now covers unsupported names too.
- A new file below a folder that is not watched is found by the next scan only.
- Registering around gaps costs one native watch per readable sibling of each
  ancestor of a gap instead of one recursive watch.
