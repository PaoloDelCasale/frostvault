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

## Amendment: a faster traversal (#696, 2026-11-15)

On the same `shfs` Vault the traversal saw ~2,200 files per minute (96,500 in
45 minutes) while `find` listed the share, size and mtime included, in 14.5
seconds. Reads were 1–7 MB/s: the time went to metadata round trips through
FUSE. Per file the walk made one `stat` per active ancestor folder (4–5 for
`library/<user>/<year>/<date>/file`), an `lstat`, and an `open` + `fstat` +
`read(1)` + `close` readability probe, before the verification read the same
file again in full.

**The ancestor fence runs once per folder.** The walk checks the identity of
the folders on its current path before it lists a folder and once that
folder's entries are done, not before every entry. A folder replaced while
its entries are yielded is still left and named at the end of that folder,
and its observations are compensated and walked again (ADR-0043). The check
before each catalog batch (`validate_active`) and the whole fence at the end
of the walk (`validate`) are unchanged.

**The readability probe is left to the verification.** The walk reuses the
listing's `lstat` and skips the probe for a file the verification will hash:
no fingerprint in the catalog, or a size or mtime that differ from it. The
catalog state is looked up in bulk, once per listed folder. A fingerprinted
file whose size and mtime did not change is still probed, so a file made
unreadable after it was protected is still found by a rescan (since #709 that
probe opens the file and reads nothing, see below). A file the
verification cannot read is reported by its path as before
(`file_unreadable`, the scan is incomplete), and the scan then undoes its
observation by the same compare-and-swap restore as a replaced folder: like a
file whose probe failed, it is not observed by that scan, so it is not
catalogued or admitted for upload, and it joins the gaps the watcher
re-checks. Watcher batches keep the probe.

**Traversal progress.** During `scan_phase: traversal` the runtime status
reports the files seen so far and their rate per minute
(`runtime.traversal_files_seen`, `runtime.traversal_files_per_minute`), and
the Vault header shows them. When the catalog already holds Local Copies
from a previous scan, their count (`runtime.traversal_files_expected`) gives
an estimate of the time left; a first adoption shows none. Since #707 it also
reports the seconds spent waiting for the Vault row before each batch write and
writing the batches (`runtime.traversal_lock_wait_seconds`,
`runtime.traversal_write_seconds`); the rest of the elapsed time is the walk.
At the end of the walk the same split is logged once, with the files seen and
their rate. The production entrypoint sets the GIL switch interval to 1 ms
(default 5 ms): the walk releases the GIL at every filesystem call and,
behind the upload workers' Python work, waited up to 5 ms for each one.
Since #711 the verification phase also reports its pace in bytes once it has
run a minute (`runtime.verify_bytes_per_minute`), and the Vault header shows
the scan as its phases in order (reading folders, verifying fingerprints,
finishing), each with its state, progress bar where a total is known, and
time left.

**An unchanged fingerprinted file is opened, not read (#709, 2026-11-16).**
Accepted by the Owner. With #707 fixed, the same Vault walked ~2,000 files per
minute while uploads kept both array disks at 88–98% utilisation: ~116,000 of
its 139,000 files were fingerprinted and unchanged, and the `read(1)` of each
probe waited for a data seek (median 191 ms, p90 551 ms, measured during the
uploads), one file at a time. That probe now opens the file (`O_NOFOLLOW`),
compares the opened identity with the listing's `lstat` and closes it: a
permission change is still refused by the open and reported as before, but no
data block is fetched. A data read error is left to the next read of the file
(upload, verification, audit). Watcher batches and the other callers keep the
full probe. No scan priority over uploads: the walk no longer reads data. The
runtime status and the end-of-walk log line also report the seconds the walk
spent classifying and opening entries (`runtime.traversal_probe_seconds`, part
of the walk time).

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

## Amendment: process shutdown (#696, 2026-11-15)

Accepted by the Owner. `docker compose stop` during a large adoption failed the
worker drain ("Background workers exceeded the shutdown drain budget", exit 1).

- At process shutdown a running full scan stops cooperatively at the next
  batch boundary, in every phase: the traversal commits the batch in progress
  when its folder identity still holds, the verification publishes the digests
  already hashed (by the same compare-and-swap), the cloud listing stops between
  pages and batches.
- Reads not yet started are cancelled and no new one is submitted; a running
  read aborts between chunks and is awaited.
- What is committed stays exactly as after a crash: no journal compensation,
  no missing transition, no finalization, no scan success and no error or
  Problem (a shutdown is not a failure). The next start rescans as before; it
  hashes only the rows without a fingerprint, so published digests survive.
- Scheduled maintenance (`scan_all_vaults`, audits, cloud recovery scans)
  starts no further Vault once shutdown is requested; the paired scans of the
  watchers are awaited by the worker drain like maintenance work. The drain
  budget is unchanged.
- No traversal checkpoint and no schema change: a stopped traversal is walked
  again from the start at the next scan.

## Amendment: part digests for multipart uploads (#703, 2026-11-15)

Accepted by the Owner (#700, proposal 3). The verification had just hashed a
Local Copy when its upload, on the same generation, size and mtime, read the
whole file again to build its Upload Snapshot, then a third time to send it.

- **The same read records the parts.** When the verification hashes a Local
  Copy of at least `MULTIPART_THRESHOLD` (8 MiB), it also records the SHA-256
  of each multipart part, cut with the Upload Snapshot's part size for that
  size (`upload_part_size`, the `ChunksizeAdjuster` rule on
  `DEFAULT_PART_SIZE`). They are stored next to the digest
  (`files.local_part_size`, `files.local_part_sha256`, migration 0004) and
  published in the same compare-and-swap on generation, size and mtime.
  Smaller files record none. NULL means not recorded, including every file
  verified before this revision; nothing is backfilled.
- **Never parts of other content.** Every path that clears or replaces the
  digest clears the parts in the same statement: a new stat or a content
  event, a replaced or released Local Copy, a Reconnect, a recovery, the
  journal restore (which restores both). A digest published without parts
  keeps the recorded ones only when it is the same digest (the same bytes),
  otherwise clears them. A database trigger drops the parts of any digest
  that changes without new parts, and a check keeps them next to a digest,
  for 8 MiB or more, one per part of the recorded size.
- **The upload builds its snapshot from them.** An upload Job of a file of
  8 MiB or more, with no Archive Version linked and no snapshot bound (so not
  a retry after a send), whose observation carries the digest and its parts
  for the run's generation, size and mtime, builds its Upload Snapshot from
  the catalog: digest, parts and part size, with mtime and ownership from
  the stat the run observed. It reads nothing and takes no preparation
  reader permit. The file is then read once, by the send, which checks each
  part against the snapshot before sending it, as before (ADR-0034). A
  fingerprint equal to Current's is not used: a match with Current is still
  proven by reading the file. Missing parts mean the upload hashes as before.
- **A stale fingerprint is retired, without cycles.** If the content changed
  without changing size or mtime, a part read by the send does not match:
  the send fails as a source change after its send-start. The run then
  retires that fingerprint by compare-and-swap on the generation, size,
  mtime and digest (digest, parts and match cleared, generation unchanged),
  so the Local Copy is verified again. The retry handles the started send as
  ADR-0030/0036 describe: it reconciles it (the multipart upload is aborted,
  absence proven, the attempt settled as a no-write), and since the Job is
  bound to snapshot A it hashes the whole file and fails as a source change,
  which closes A; the next attempt hashes again and sends the new content. A
  whole read that contradicts recorded parts of the same observation also
  retires them, so a retirement that did not commit cannot bring A back. A
  retry never takes its snapshot from the catalog.

Consequences: a stale fingerprint now costs at most the two stability
windows of a change during a transfer (ADR-0030), and its retirement takes
the Local Copy out of "verified" until the next scan or upload hashes it.
The per-part digests add CPU to the verification read (one more SHA-256 of
the same bytes), not I/O.
