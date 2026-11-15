# Local release blocked by path; DAC override by opt-in

Status: accepted by the Owner on 2026-11-15 (issue #681).

## Context

Running as root (#679) makes files of other owners that root may not read
visible to the scan as unreadable paths. Before this decision any unreadable
path or unsupported name, anywhere in the Vault, left the whole Vault
unconfirmed: one private file of another user blocked freeing local space on
every other file, and the 409 did not say why.

Root without `CAP_DAC_OVERRIDE` also cannot create, rename or remove entries
in folders of other owners (`99:100 0755`, the typical Unraid share), so
Recovery in place, Local Copy cleanup and reconnect failed with a raw
`Errno 13`, a Deep Archive Recovery even after hours of S3 restore. The
entrypoint refused `CAP_DAC_OVERRIDE` and `CAP_DAC_READ_SEARCH`
(docs/filesystem-permissions.md, #679).

## Decision

**Local release is gated by path.** An unreadable path or an unsupported name
is named by its own Problem. It blocks local release (manual free-space, the
automatic local cleanups and the worker's re-check, consistently) only for that
path, everything below it and the folders above it, which contain it. The rest
of the Vault can be released. The whole Vault stays blocked for global causes,
where no path names the gap: Source Volume unavailable or replaced, root
identity mismatch, an inaccessible Vault root, a scan or watcher failure without
a named path (an undecodable name the watcher stopped on, before the scan names
it), watcher registration pending, a failed cloud scan, a failed catalog audit,
a non-active lifecycle. An incomplete source observation without any open path
Problem also blocks the whole Vault (fail closed). The 409 names the blocking
path and points to its Problem.

The trade-off: a file that the scan could not see because it was renamed to an
unsupported name, or that sits in an unreadable folder, may be the new name of
a released file. That does not release anything unverified: the worker still
checks the exact cloud version and the local digest of the very file it
releases. What stays out of scope is cloud-side deletion: Cleanup and Delayed
Cloud Deletion keep requiring the whole Vault confirmed, because there the
missing observation could hide the only local copy.

**`CAP_DAC_OVERRIDE` and `CAP_DAC_READ_SEARCH` by explicit opt-in.**
`FROSTVAULT_ALLOW_DAC_OVERRIDE=1`, one variable for both, allows them, only with
`PUID=0 PGID=0`; without it the entrypoint keeps refusing them, and it refuses
the variable with any other identity. `CAP_FOWNER`, `CAP_SYS_ADMIN` and a missing
`no-new-privileges` stay refused. When the capabilities are held the startup log
and the System page say so. This amends the #679 rule; it is meant for shares
with several owners, where root without them is worse than `99:100`.

**Writability is checked before local work.** Recovery (at submission and before
asking S3 for a restore), Local Copy cleanup and reconnect check that the folder
they write is writable and searchable by the effective identity, as the kernel
decides (capabilities and groups included). If not, the request or Job says which
folder, its owner and mode, and FrostVault's identity; an unexpected `EACCES`
during those operations gives the same message. A Recovery that fails after its
restore completed says until when the restored copy is kept and that Recover
reuses it.

**Recovery of a version without recorded owner** (archived before v0.8) gives the
file the owner of its destination folder; the mode is unchanged. Without
`CAP_CHOWN` it keeps the process identity, never failing the Recovery.

## Displayed state

Amended on 2026-11-15 (issue #684). The protection state shown in browse, file
detail, folder badges, Vault statistics and the weekly summary follows the same
rule as the release gate, from one shared function
(`operation_policies.observation_blocking_paths`, wrapped by `UnconfirmedScope`):
a file at, below or above a blocking path is shown unconfirmed, with its last
known state; every other file shows its real state. A folder at, below or above
a blocking path is shown unconfirmed as a whole, like the gate refuses to release
it as a whole; the Vault itself, the root above every path, is too, while its
counters show each file's own state. The global causes above keep the whole
Vault unconfirmed. The scoping is a SQL predicate on the path, so counting and
filtering stay in SQL on large Vaults. The Vault health (`runtime.last_error` of
the stats) says how many paths are unreadable and points to their Problems
instead of the generic incomplete-scan error.

## Consequences

A Problem that is resolved while the scan still reports the source incomplete
leaves no path to scope by, and the Vault is blocked, and shown unconfirmed, as
a whole until the next scan.
