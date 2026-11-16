# Automatic Duplicate Cleanup is opt-in, daily and capped

Status: accepted by the Owner on 2026-11-16 (issue #726). Builds on Delayed
Cloud Deletion and the Cloud Duplicate definition of #723; manual deletion is
unchanged.

## Context

Apps such as Immich move and rename files: the old path becomes Cloud-only
while the same content is protected again at its new path. These Cloud
Duplicates cost storage without protecting anything, and #723 only lets the
Owner delete them by hand («Elimina dal cloud» with «Solo doppioni»). On a
large library that is a recurring chore.

## Decision

- **Opt-in per Vault, off by default.** A backup must never delete by default.
  Turning the cleanup on, or changing its wait or caps, needs a fresh
  authentication, like any other cloud deletion consent; turning it off does
  not, because it removes no protection.
- **Only Cloud Duplicates.** The candidates are exactly the files the manual
  «Solo doppioni» filter selects. A Cloud-only file without a protected copy
  at another path is the only copy of its content and is never scheduled
  automatically. Files already in a pending or active deletion are skipped.
- **Grace from the last local sighting.** A duplicate qualifies only when its
  Local Copy was last seen at least the configured number of days ago
  (1–90, default 7), so a move done in several steps can finish first. A file
  never seen locally does not qualify.
- **Daily, persisted.** Each active, unpaused Vault runs at most once a day; the
  last run is stored with the Vault, so a restart does not run it early.
- **One ordinary deletion per run.** A run schedules at most one Delayed Cloud
  Deletion through the same path as the Owner's, with the same seven-day
  cancellable delay and execution checks, marked automatic and announced by a
  notification and an audit event.
- **A cap opens a Problem.** If the candidates exceed either cap (files or
  bytes; defaults 5,000 and 50 GiB), the run schedules nothing and opens a
  Vault Problem pointing to the manual flow: an unexpected burst, such as a
  whole library reorganised at once, is reviewed by a person instead. The
  Problem closes at the next run under the cap, or when the
  cleanup is turned off.
- **Per-file revocation only for automatic groups.** At execution, or when a
  file reappears, a member of an automatic deletion that is no longer valid
  (back on the server, no longer a Cloud Duplicate, changed or busy) is
  skipped alone and recorded; the rest proceed. A manual deletion stays
  all-or-nothing: the Owner confirmed that exact set, and deleting a subset of
  it would act on a consent the Owner never gave. The automatic set was never
  confirmed as a whole; each member stands on its own Cloud Duplicate proof.
  Problems that affect the whole Vault (root, mount, lifecycle, unconfirmed
  observations) still cancel the whole automatic group.

## Consequences

With the cleanup on, Cloud Duplicates leave the cloud about a week after the
grace period without the Owner's action, while every deletion stays visible
and cancellable for seven days. A cleanup left on can only remove content that
is still present and protected elsewhere in the same Vault.
