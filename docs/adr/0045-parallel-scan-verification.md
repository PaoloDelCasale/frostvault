# Parallel verification of a full scan

Status: accepted by the Owner on 2026-11-15 (issue #691).

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

## Consequences

- A full scan holds up to `SCAN_HASH_CONCURRENCY` file descriptors and reads of
  the source at once; on an SSD the gain is small, on a single slow disk the
  queued reads compete with uploads reading the same disk.
- The scan still holds the Vault scan lock during the verification, so
  watcher batches and automatic upload admission of that Vault wait for it.
