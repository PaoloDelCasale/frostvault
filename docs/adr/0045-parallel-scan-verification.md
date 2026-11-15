# Parallel verification of a full scan

Status: accepted by the Owner on 2026-11-15 (issues #691 and #689). Amends the
scan lock of [ADR-0043](0043-replaced-folder-invalidates-its-subtree.md),
whose uploads still waited for the scan to finish.

## Context

After its traversal, a full scan computes the content fingerprint of every
present regular Local Copy without one (the verification phase). It did so one
file at a time. On a large adoption from an HDD array behind a FUSE union
(Unraid `shfs`: 138,822 files, 533 GiB) a single read ran at ~14 MB/s: the
traversal ended in about an hour, the verification needed 10–12 more, and for
all that time the Vault stayed unconfirmed and gave no sign of progress. The
`source` error of an older scan was shown meanwhile as if it were current.

## Decision

**Bounded parallel reads.** The verification hashes up to
`SCAN_HASH_CONCURRENCY` Local Copies at once (environment variable, default 4,
range 1–32), on a pool owned by that scan. With several reads queued the disk
and the FUSE layer can order them; 1 keeps the old sequential behaviour. At
most twice as many reads are queued as run, so a scan that is superseded or
loses its root identity cancels the reads not yet started and waits only for
the running ones. Uploads and Recoveries have their own limit
(`OPERATION_CONCURRENCY`); the two are not shared.

**Published as results arrive.** Digests are published in
`LOCAL_SCAN_WRITE_BATCH_SIZE` compare-and-swap batches as soon as they
complete, not once at the end: each committed batch is durable progress. The
compare-and-swap is unchanged: a digest publishes only on the generation, size
and mtime read before hashing, and each batch first checks the enrolled root
and that the scan was not superseded. A watcher replacement or a compensated
subtree (ADR-0043) changes the generation and wins. Unreadable files and
Problems are recorded by the scan thread, never by the hashing threads.

**Progress in the runtime status.** The running scan reports its phase
(`traversal`, `verification`, `finalization`) and the files and bytes verified
out of the total in the process-local runtime status of the Vault, exposed by
`GET /api/stats` (`runtime.scan_phase`, `runtime.verify_*`) and shown in the
Vault header. Nothing is persisted: after a restart the next scan reports its
own progress.

**An older `source` error is the last scan's outcome.** While a new scan runs,
a `source` error and its `protection_blocked` Problem from a previous scan stay
open: protection is not confirmed before the new scan succeeds. The status
reports when the error was recorded (`runtime.last_error_source_at`, from
`vault_component_health.updated_at` after a restart), and the Vault header
shows it as "last scan failed" with that time, next to "new scan in progress".
The error and the Problem close when a scan succeeds, as before.

**Verification without the Vault scan lock (#689).** The traversal holds the
Vault scan lock; the verification releases it; the finalization (missing
transitions, renames, relocation, Problems) takes it again. Meanwhile watcher
batches, Local Copy cleanup and Recovery publication of that Vault run, and
automatic upload admission too. A second full scan of the Vault is refused
while one is verifying (the runtime status still says `scanning`). The
finalization re-checks the enrolled root, the lifecycle and the scan
generation before writing, as before: a scan superseded or whose root changed
in between rolls back its journal as today. A file gone since the listing
(a cleanup claim, a watcher deletion) is skipped, not reported unreadable.

**Admission during the verification.** While a full scan of the Vault
verifies, automatic admission takes only Local Copies whose fingerprint was
published in a committed batch (by this scan or earlier) and that are stable
per the policy; it does not hash new files itself, the scan does. Each Job
admitted then records the generation of its File (`jobs.admitted_generation`,
migration 0003; NULL outside a running scan). A compensated observation is
never authority for an upload: the journal restore makes it a new generation,
including a file the scan created and could not delete because the Job is
attached to it, and a Job whose File no longer has its admitted generation is
cancelled when it is claimed (`job.upload_observation_withdrawn`), before any
read or send. Automatic admission then decides again on the current
observation. A newer watcher observation fences the Job the same way. The
upload keeps its own Upload Snapshot and fences as before.

## Consequences

- A full scan holds up to `SCAN_HASH_CONCURRENCY` file descriptors and reads of
  the source at once; on an SSD the gain is small, on a single slow disk the
  queued reads compete with uploads reading the same disk.
- During a large adoption uploads start while the scan verifies, up to
  `OPERATION_CONCURRENCY`; they read the same disk as the verification.
- A Job admitted during a scan is cancelled, not rescheduled, when its
  observation changes; its file is queued again by the next admission pass.
- Changes made while the scan verifies are applied by the watcher at once
  instead of after the scan.
