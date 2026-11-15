# A folder replaced during a scan invalidates only its subtree

Status: accepted on 2026-11-15 (issue #686). Amends the scan fence of #593 and
extends [ADR-0042](0042-local-release-blocked-by-path.md).

## Context

The scan walks a Vault with pinned directory descriptors and, at the end,
checks that every folder it visited still has the same `(st_dev, st_ino)`.
Until now a single replaced folder anywhere raised `TreeIdentityChanged` and
the whole scan journal was rolled back. On a live source this never ends: Immich
removes its emptied `upload/<user>/<xx>/<yy>/` folders and creates them again
when new photos arrive, so a 30-minute scan of a 139,000-file Vault always met
one, the catalog was emptied after every attempt and no upload ever started.

The fence exists because observations made through a folder that was replaced
(renamed away, removed, swapped for another tree) may describe the old folder,
not what the path holds now. That risk is confined to the folder's own subtree.

## Decision

**Two scopes.** The Vault root, and for a watcher's subtree walk the folders
leading to its start, prove the walk as a whole: their replacement still raises
and the whole journal is rolled back, as before. Any folder below them that is
replaced, removed, turned into a file or symlink, or made inaccessible during
the walk is a *replaced subtree*: the walk leaves it and goes on with its
siblings, and only the topmost replaced folder is named.

**Compensate and walk again.** The scan restores the journal entries at and
below each replaced folder (the same compare-and-swap restore as a whole
rollback) and walks that folder again in the same scan, up to
`SCAN_REPLACED_FOLDER_REWALKS` (3) times. A folder that is gone is an empty
subtree, so its old Local Copies are marked missing by the complete scan. A
file that disappears between the listing and its stat is skipped.

**A folder that keeps changing** after the re-walks keeps its last known catalog
state, gets its own path Problem (`folder_changed_during_scan`) and makes the
observation incomplete. Like an unreadable path (ADR-0042), it leaves protection
unconfirmed only there, below it and in the folders above it; missing-copy
transitions wait for a complete scan. The next whole scan that observes the
folder closes the Problem.

Each replaced folder writes one warning line to the log, with the Vault and
the path. A walk without a scan journal to compensate (no production caller)
still refuses as a whole, naming the folder.

Since #696 the walk checks the folders on its current path at folder
boundaries (before a folder is listed and when its entries are done), not
before every entry; see the amendment to
[ADR-0045](0045-parallel-scan-verification.md).

## Consequences

A replacement after a folder was re-walked but before the scan finished is not
seen by that scan; it is seen by the watcher or the next scan, like any change
made after the walk passed a folder.

Out of scope, follow-ups: exclusion globs do not prune the walk (excluded
files are still catalogued and shown as excluded, ADR-0028; since resolved by
[ADR-0044](0044-excluded-folders-are-not-observed.md)), and uploads still
wait for the scan to finish.
