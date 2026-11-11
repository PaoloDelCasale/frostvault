# Original file modification times

New Upload Snapshots preserve the source file's modification time as signed Unix
nanoseconds (`mtime_ns`), captured by the same stable hashing pass as the content.
The immutable snapshot is retained in the catalog and upload journal; S3 PUT and
multipart creation also store it as user metadata `frostvault-mtime-ns`.

Recovery uses the Archive Version's snapshot, or the metadata returned by the
existing exact-VersionId HEAD if catalog timestamp evidence is absent. It applies
`os.utime(..., ns=...)` to the temporary file before hashing and verified atomic
publication. Both the Local Copy observation and fingerprint cache see the final
timestamp. The explicit `local_copies.recovered_at` still starts a fresh local
retention window, independently of the original date (#409).

Single-part and multipart storage-class copies preserve S3 metadata; a catalogless
emergency recovery can therefore read the timestamp from the exact version.
The Recovery Kit includes manual AWS CLI/Python instructions. **AWS CLI download
commands do not automatically apply this custom timestamp**; `LastModified` is an
S3 upload/copy timestamp, not the source file's mtime.

## Existing data and compatibility

This does not retroactively repair older uploads. Legacy snapshots without
`mtime_ns` retain their exact serialization and may resume without altering
immutable evidence. If neither the snapshot nor S3 metadata records the original
time, recovery retains its existing timestamp behavior: it does not guess from a
changed Local Copy or S3 `LastModified`. A cloud-only file uploaded before this fix
may have permanently lost its original mtime. No automatic backfill, re-upload or
rewriting of existing cloud versions is performed.

Precision is bounded by the destination filesystem. File ownership, modes, atime,
ctime, directory timestamps and extended attributes are outside this contract.
Malformed timestamp evidence fails recovery rather than silently publishing a
file with an invented date. Timestamp metadata is not a cryptographic integrity
proof; existing exact-version SHA-256 verification is unchanged.
