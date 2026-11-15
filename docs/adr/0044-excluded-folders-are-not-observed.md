# Excluded Folders are not observed

Status: accepted by the Owner on 2026-11-15 (issue #688). Supersedes, for
Excluded Folders only, the part of [ADR-0028](0028-excluded-protection-state.md)
under which every excluded file is still catalogued; closes the follow-up left
by [ADR-0043](0043-replaced-folder-invalidates-its-subtree.md).

## Context

Until now an exclusion only kept files out of automatic upload: the scan still
entered, listed and fingerprinted every folder, and the watcher watched it. On a
live source this costs the most exactly where the Owner asked for nothing:
Immich's `upload/<user>/<xx>/<yy>/` churn, `node_modules`, caches and backup
dumps are walked on every scan, hashed, and hold one native watch per folder.
Excluding `upload/**` did not even avoid the folder replacements of #686.

## Decision

**Which rules make an Excluded Folder.** Only a Vault `exclude_globs` entry that
ends in `/**` and has one of two forms: `dir/**` (anchored at the Vault root) and
`**/dir/**` (anywhere). The folder part may span several segments and use `*`
and `?` within a segment; it may not use `**`, `[...]` sets or a backslash, and a
glob the SQL projection cannot classify (ADR-0028) never prunes. With these
limits a folder whose segments match the rule's segments one by one has every
descendant excluded by admission and by the SQL projection alike: the rule is
certain, never approximate. Every other pattern (`*.tmp`, `**/x/*.jpg`) and the
fixed noise exclusions (`._*`, `.DS_Store`, …) keep cataloguing exactly as
before. `include_globs` do not matter: admission applies the exclusions after the
inclusions, so nothing an include matches can re-open an excluded path. A folder
whose name contains a backslash is never pruned, since admission reads `\` as a
separator.

**Not entered.** Full scans, the re-walks of replaced folders and the watcher's
subtree walks do not open, list, fence nor hash an Excluded Folder. A walk that
starts inside one yields nothing.

**Known rows freeze.** Rows already catalogued below an Excluded Folder (the
glob was added later) keep their last known state. The full scan's missing
transition and a watcher's folder removal skip them, using the rules the walk
pruned with, and they are not fingerprinted again. They are shown as the
protection state ADR-0028 derives (normally Excluded) plus a "not observed:
excluded folder" indication (`in_excluded_folder` in the file API), derived at
read time from the current policy: no column, no migration, no cloud data
touched.

**Not watched.** The scan records the Excluded Folders it met in the watch plan
(`watch_plan.UnobservedPaths.excluded`, also in `directories`), and the watcher
registers around them as it does around unreadable folders (ADR-0027): ancestors
without recursion, siblings recursively. Excluded Folders are not a gap: they do
not make a scan incomplete, the Vault unconfirmed, nor trigger re-checks. A
watcher batch ignores every event inside one; a new folder that a rule covers is
added to the plan at once, so the watcher registers around it before any scan.
An Excluded Folder removed from disk stays in the plan while its rule holds, so
its disappearance is not mistaken for a coverage gain worth a scan.

**Removing or narrowing a rule.** When a policy save drops a folder rule, the
folders it pruned leave the watch plan at once. If a live watcher had registered
around one of them, its new registration expands coverage and the watcher pairs
it with a full scan (ADR-0027). Otherwise a full scan is requested directly,
now or right after the one in progress. Either way the folder's files are
catalogued and those gone from disk become missing. Adding a rule starts no
scan: the rows freeze, the watcher ignores the folder's events at once and
registers around it after the next scan.

## Consequences

- Deleting an Excluded Folder, or a file in it, on disk is not detected while
  the rule stays: its rows keep their last known state. Removing the rule makes
  the next scan see it. The Details text says so.
- A file added to an Excluded Folder is not catalogued, so it has no row at all;
  only what was known before the rule is shown.
- A Vault reconnect (ADR-0014) still discards every old local observation; the
  reconnect scan does not enter Excluded Folders, so their rows stay absent until
  the rule is removed.
- A manual upload of a frozen row is still possible (ADR-0028); it reads the
  file on disk as any manual upload does.

## Considered options

- Pruning on any exclude glob that matches the folder's own path: a rule such as
  `**/x/*.jpg` covers only some files of `x`, so the rest would vanish unseen.
- Removing the known rows from the catalog: loses the explanation for files that
  may have a cloud copy, and turns a rule change into catalog deletion.
- Letting the rows become missing: would make them cloud-only and deletable from
  the cloud for a folder that is still on disk.
- A persisted per-file flag: needs recomputation on every rule change and can
  disagree with the policy; reading the policy cannot.
