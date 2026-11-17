# Mirror Cleanup follows server deletions to the cloud, opt-in, daily and capped

Status: accepted by the Owner on 2026-11-17 (issue #771). Builds on Delayed
Cloud Deletion and on the engine of Automatic Duplicate Cleanup
([ADR-0048](0048-automatic-duplicate-cleanup.md)); manual deletion and the
duplicate cleanup are unchanged.

## Context

Some Vaults hold data that is already a backup: rotating database dumps, Gaia
or appliance exports kept for a few weeks and then replaced by the producing
tool. On the server the old files disappear; in the cloud they stay as
Cloud-only files forever, because FrostVault never follows a server deletion
on its own. The Owner then deletes them by hand with «Elimina dal cloud», a
recurring chore that grows with every rotation.

The Automatic Duplicate Cleanup cannot help: it only takes Cloud Duplicates,
whose content is still protected at another path. A rotated dump is the only
cloud copy of its content.

## Decision

- **A separate concept, Mirror Cleanup** («Elimina dal cloud i file spariti dal
  server»). It has its own settings, status, totals, notification, audit events
  and Problem, beside the duplicate cleanup, and reuses its engine (candidate
  classification, daily run, caps, scheduling through Delayed Cloud Deletion).
  It is kept separate because it deletes the **only** cloud copy: the duplicate
  cleanup's safety argument ("only content still protected elsewhere") does not
  hold, and the two must be switched on and reasoned about independently.
- **Opt-in per Vault, off by default**, with an explicit warning in the UI: the
  cloud no longer protects files deleted from the server, so it does not guard
  against accidental deletion and suits copies that are already backups, not
  original data.
- **Candidates.** Cloud-only files whose Local Copy was last seen at least N
  days ago (1–180, default 7). Files never seen locally are never taken. Cloud
  Duplicates are included. Files already in a pending deletion, including one
  scheduled by the duplicate cleanup, are skipped, so the two cleanups never
  schedule a file twice; runs of the same Vault are serialized by the Vault row
  lock.
- **Optional scope.** A list of folders relative to the Vault root (at most
  100), normalized and validated: no globs, no `..`, no absolute paths; nested
  entries collapse into their parent. An empty list means the whole Vault.
- **Daily, persisted.** Each active, unpaused Vault runs at most once a day; the
  last run and its outcome are stored with the Vault (migration 0017).
- **One ordinary deletion per run.** A run schedules at most one automatic
  Delayed Cloud Deletion group (marked `mirror_cleanup`) with the seven-day
  cancellable delay, the execution re-checks, a notification and an audit event.
  A file under Retention Lock waits for its RetainUntilDate as in every other
  deletion.
- **A cap opens a Problem.** Above either cap (defaults 5,000 files and 50 GiB)
  nothing is scheduled and a Vault Problem opens, pointing to the manual flow.
  It closes at the next run under the cap, or when the cleanup is turned off.
- **Per-file revocation.** At execution, or when a file reappears, a member back
  on the server, changed or busy is skipped alone; the others proceed. Unlike
  the duplicate cleanup there is no Cloud Duplicate check, since being the only
  copy is the point. Vault-wide problems still cancel the whole group.
- **Beyond the minimum.** «Solo oltre la durata minima» works as in #731: off by
  default; a file still within its Minimum Storage Duration stays until it ends.
  The panel estimates the cloud life as N + 7 days after the disappearance and
  compares it with the minimum of the class the Storage Plan has the file in at
  that age.
- **Fresh authentication to loosen, never to tighten.** Enabling, a shorter
  wait, higher caps, a scope not covered by the previous one, or turning off
  «beyond the minimum» needs a fresh authentication. Disabling, narrowing the
  scope, a longer wait, lower caps or turning «beyond the minimum» on do not.
- **Guided profile «Backup recenti»** (`recent_backups`): GLACIER_IR from day 0,
  for rotating backups kept a few weeks and restored at once; it pairs with
  Mirror Cleanup but is independent of it.

## Consequences

With Mirror Cleanup on, a file deleted from the server leaves the cloud about
N + 7 days later without the Owner's action. Every deletion stays visible and
cancellable for seven days, and a burst (a mistaken `rm -r`) above the caps is
held for a person. Within that window, and only within it, the cloud still
protects against the accidental deletion; after it the content is gone. Owners
who need the cloud as protection for originals must leave it off.
