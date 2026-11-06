# Independent SHA-256 ingestion proof for new uploads

Issue #460 accepts S3's cryptographic ingestion proof as sufficient Integrity
for new Upload Snapshots, instead of paying for an independent full Read-back
on every upload. This supersedes **only the new-upload read-back-only and CRC32
proof requirements** in ADR-0016/0018; sole-copy safeguards, immutable exact
identities, completed-after-cleanup and all other operations remain unchanged.
ADR-0009/#416 still prohibits a mutable global settings UI.

## Consequences

`UPLOAD_VERIFICATION` supplies the deployment default (`s3_checksum`, default,
or `read_back`). Each Vault can inherit (including all existing/new Vaults) or
override it for **new Job admissions**. A Job freezes the requested mode; its
Upload Snapshot independently persists plaintext SHA256, size, SHA256 algorithm,
FULL_OBJECT/COMPOSITE type, expected checksum and multipart boundaries/part
checksums before writing cloud content. The immutable Archive Version retains
that baseline and its effective verification method/fallback. Later policy edits
and Local Copy changes cannot rewrite historical evidence. Old versions with no
independent S3 baseline require exact Read-back against the durable plaintext
digest; never reconstruct expectations from HEAD or a changed Local Copy.

A stable hashing pass derives both proofs from the same content. Before sending,
the classic managed-transfer facade reads each part into a bounded buffer and
checks its SHA256 against that independent part baseline. PUT supplies full
SHA256. Multipart uses consecutive part numbers, independently supplied SHA256
part checksums and the SHA256 of their binary digests with `-N` suffix (COMPOSITE),
not plaintext SHA256. There is no whole-file temporary copy or Vault artifact;
normal parts are 8 MiB, adjusted up to a hard 1 GiB for S3's 5 TiB/10,000-part
limits, at most two buffers per Job. Hash reads are 1 MiB. SDK seek/retry,
cancellation, throttled progress and the process-wide shared bandwidth limiter
are retained on the actual transmitted buffers. A changed unread part aborts
before it is sent; verified snapshot A never protects later Local Copy B.

The response's exact VersionId is captured at the facade and registered/linked
before subsequent HEAD or proof checks. A valid full/composite response (or an
exact checksum-enabled HEAD) establishes provider **ingestion**, not an
independent observation of stored bytes through GET. It confers verified and
permits the usual guarded Current Snapshot cleanup/local release. Checksum/type,
size or exact-VersionId contradictions block with no silent downgrade. Missing
proof or a specifically unsupported checksum HEAD API requires visible exact
GET/SHA256 fallback and associated retrieval/egress charges. Authentication,
connectivity and other failures are not reinterpreted as lack of capability.
Providers refusing checksum-bearing writes fail safely; transport does not blindly
repeat an ambiguous PUT without checksum. Completed versions are never deleted
by transport abort cleanup.

Rename, Move now, Recovery and Reconnect retain their existing independent/full
verification contracts; the new-upload policy does not extend to them. Incomplete
multipart still uses explicit abort and the existing seven-day lifecycle rule.
This PR does **not** close the process-crash/ambiguous-response/database-journal
gap, adopt historical orphans or add operational recovery (#464/#459).

## Sources and pinned seam

- AWS [checking object integrity](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html): SHA256 multipart is COMPOSITE, not FULL_OBJECT.
- AWS [UploadPart](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UploadPart.html), [CompleteMultipartUpload](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html), [HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html): explicit part checksums, composite/type and checksum-enabled exact-version queries.
- Pinned `s3transfer==0.19.3`, `upload.py::_submit_multipart_request` forcibly selects FULL_OBJECT when explicit ChecksumSHA256 is passed to manager.upload. We pass the algorithm/type only; the narrow client facade supplies independently expected PUT/part/completion fields. Its ReadFileChunk/InterruptReader wrapper seam is pinned and tested; SDK upgrades must rerun the wire/buffer/cancellation/shared-budget tests.
