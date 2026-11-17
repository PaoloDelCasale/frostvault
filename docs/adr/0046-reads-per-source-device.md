# Reads per Source Device

Status: accepted by the Owner on 2026-11-16 (issue #713). Adds to the bounded
reads of [ADR-0045](0045-parallel-scan-verification.md) and
[ADR-0034](0034-bounded-upload-execution.md); evidence, fingerprints and
upload authority are unchanged.

## Context

On 0.10.0 a Vault of 139,270 files (534 GiB) was being adopted from an Unraid
user share (FUSE `shfs`) whose files are spread over two HDDs of
the array, with `OPERATION_CONCURRENCY=16` and `SCAN_HASH_CONCURRENCY=8`.
CPU, memory and PostgreSQL were no longer the limit (FrostVault ~15% of one
core); uploads ran at 1.5–1.7 GiB/min on a 500 Mbit/s link and the scan
verification at ~870 MiB/min. `iostat` showed one disk at 100% utilisation
with reads of ~112 KB each waiting 60–95 ms, while the other disk was idle,
then the reverse a few minutes later. The host reached load 26 and 63%
iowait, and Plex and Immich, reading the same disks, slowed down.

The files to read were on both disks (a sample of 2,000 queued uploads:
1,367 on disk1, 612 on disk2), but every reader followed path order: up to 8
hashers and 16 uploads read different files of the same HDD at once, the
head seeked between them, and beyond 2–3 streams an HDD delivers less, not
more. A file without a fingerprint was also read twice, once by the
verification and once by its upload.

## Decision

**Source Device of a file.** On Unraid `shfs` the extended attribute
`system.LOCATION` names the array disk that holds a file (`disk1`, `disk2`,
`cache`); reading it fetches no data. Where it is not supported the device
is the file's `st_dev`. The answer is cached per parent folder in a bounded
cache, since Unraid keeps most of a folder's files on one disk. A wrong
guess costs only scheduling efficiency; a failed lookup yields "unknown" and
never fails an operation.

**A read limit per device.** `SOURCE_DEVICE_READ_CONCURRENCY` (default 3,
range 1–16) bounds the reads of Local Copies in flight on each device, the
cache included. It applies in addition to `SCAN_HASH_CONCURRENCY` and
`OPERATION_CONCURRENCY` and one process-wide registry is shared by scan
verification and uploads. A slot is held only while a Local Copy body is
read: the verification's hashing pass, the upload's hashing pass, and the
send's read of each multipart part (or of a single-PUT body it did not keep),
never while waiting on the network. Waiting for a slot runs the waiter's
checkpoint every half second: process shutdown, a superseded or closed scan,
a cancelled Job or a lost claim stop the wait. A closed scan still waits only
for the reads already running (ADR-0045). The registry reports the reads
running and waiting per device in `GET /api/stats`
(`runtime.source_devices`, `runtime.source_device_read_limit`).

**Rotation.** The scan verification groups the files it still has to read by
device and submits them round-robin, at most the device limit per device, so
a busy disk does not starve the others. When claiming, the scheduler looks
at a window of 64 ready Jobs per Vault in the existing order and takes the
first upload whose device has a free slot (reads in flight plus uploads
already chosen in that pass); if none is free, or devices are unknown, the
existing order holds. A Job that is not an upload is never overtaken. No
schema change: the device is resolved at claim time from the cache.

**One read for files the scan just hashed.** When the verification hashes a
Local Copy, its file joins a process-local, bounded set (1,024 files, 5
minutes). A claimable upload of such a file joins the candidate window even
beyond its usual cut and goes first, so its read is served by the page cache.
Nothing else changes: the verification still records the fingerprint, the
upload still builds and checks its own Upload Snapshot.

**Bigger reads.** Local Copies are read 4 MiB at a time instead of 1 MiB, by
the verification, the Upload Snapshot pass and the send. Part boundaries
never depended on the read size and do not now: `MULTIPART_THRESHOLD` and
`DEFAULT_PART_SIZE` stay 8 MiB, so part digests recorded by older scans
(ADR-0045, #703) still match. The payload budget of ADR-0034 keeps its
accounting of 1 MiB per live read chunk (64 MiB ordinary lane, `size + 2 MiB`
per single PUT); the larger transient read buffers are outside it, like other
Python and SDK allocations, so the budget admits as many sends as before.

**Server priority (opt-in).** With `SERVER_PRIORITY_IOWAIT_PERCENT` set
(unset or 0: off), FrostVault samples the host iowait from the aggregate `cpu`
line of `/proc/stat` every 2 seconds. After 10 seconds continuously above the
threshold it slows down: one read per device and no new upload Jobs claimed
(running ones finish; scans keep reading at one file per device). After 30
seconds continuously below it returns to normal. Transitions are logged; the
state and its start are in `GET /api/stats`
(`runtime.server_priority_throttled`, `runtime.server_priority_throttled_since`)
and `GET /api/upload-activity` (`server_priority`), and the header's upload
indicator says "Slowed down: server busy". A queue held back this way is not
reported as a Stalled Queue.

## Consequences

- On a single disk, or when every lookup falls back to the same `st_dev` (a
  FUSE union without `system.LOCATION`), the device limit is a global limit
  of reads: with the default 3, scan and uploads together read at most three
  Local Copies at once.
- `SCAN_HASH_CONCURRENCY` above the device limit times the number of devices
  adds no reads; uploads beyond the device limits wait for a slot after their
  claim, holding their operation slot.
- An upload can now be claimed out of path order within its Vault; its tier,
  the cross-Vault fairness and aged turns are unchanged.
- The device cache can be stale after Unraid's mover or a manual move; it is
  corrected as folders leave the cache, and a stale entry only misplaces a
  read in the rotation.
- With the server priority on, a long busy period on the host holds new
  uploads for as long as it lasts.

## Amendment: one read, a limit per disk type, rates per device (#766, 2026-11-17)

**Context.** During the adoption of ~52,000 Local Copies without a
fingerprint (~294 GB), all on one Unraid array HDD, three interleaved reads
fingerprinted at 17 MB/s and one at 63–117 MB/s: the head seeked between
files. Every new Local Copy whose upload ran before the verification reached
it was also read twice, by its upload and by the scan.

**One read.** When an upload's Upload Snapshot pass reads a Local Copy that
has no fingerprint, that whole, stable read (size and mtime unchanged before
and after) is its fingerprint: the digest and the part digests are published
right after the pass, before the send, through the same compare-and-swap as
the scan verification (`publish_local_fingerprint` on the generation, size
and mtime of the observation; no match with Current is inferred). A failed
send keeps it: it describes the disk, not the cloud. The observation joins a
process-local, bounded set (65,536 entries, 24 hours), and the verification
skips a row of its listing that is that very observation (same file,
generation, size and mtime), checked before and again after waiting for the
device slot. An entry serves one skip and is dropped when the fingerprint is
retired (#703), so a fingerprint cleared since is read again. The bytes the upload sends are the fingerprinted ones: a single
PUT sends the payload the pass kept, a multipart send checks every part it
reads against these part digests and a contradicted part retires the
fingerprint (#703). An upload whose snapshot comes from the catalog read
nothing and publishes nothing; there is no resumed multipart that would
leave earlier parts unread (every new send is a new attempt hashed whole).
The reverse order, the verification handing a file to its upload while it
is in the page cache, stays the "hot files" rule above.

**A limit per disk type.** `SOURCE_DEVICE_READ_CONCURRENCY` is now unset by
default. Unset, each device gets 1 read in flight when its disk is
rotational and 3 when it is solid-state or unknown, from
`/sys/block/<disk>/queue/rotational`: `dev:<st_dev>` follows
`/sys/dev/block/<major>:<minor>` from a partition to its disk and through the
members (`slaves`) of a dm/md device, rotational when any member is; an
Unraid array disk (`system.LOCATION` `diskN`) reads `mdNp1` (`mdN`), then
its member from `rdevName.N` of `/proc/mdstat`. A pool such as `cache` or a
FUSE `st_dev` is unknown. Set, the value applies to every device, as before.
The server priority is unchanged: throttled, every device gets 1.

**Rates per device.** The reads under a device slot (verification, Upload
Snapshot pass, multipart part reads) count their bytes per device; the
throughput sampler records the counters every 2 seconds and `GET /api/stats`
reports, for each device reading, waiting or with a measured rate,
`runtime.source_devices[].limit` and `.bytes_per_second` (last 20 seconds,
null while unmeasured). `runtime.source_device_read_limit` is the limit of a
device of unknown type. No schema change.
