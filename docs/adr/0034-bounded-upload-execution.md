# Keep per-file upload evidence while restructuring execution

> **Amended in part by [ADR-0038](0038-a-never-written-key-needs-no-listing-before-its-put.md)**
> (#622): a key the catalog never wrote pins a presumed-empty inventory without
> listing; permit waits are woken by releases instead of polling.
>
> **Amended in part by [#701](https://github.com/PaoloDelCasale/frostvault/issues/701)**
> (#700): stage limits derive from `operation_concurrency`, whose cap rises
> from 16 to 32, instead of fixed 2/4/4/2 defaults; the payload budgets and the
> large multipart lane are unchanged.
>
> **Amended in part by [#702](https://github.com/PaoloDelCasale/frostvault/issues/702)**
> (#700): below `MULTIPART_THRESHOLD` (8 MiB) the hashing pass is the only
> source read. Its bytes, kept in a send buffer reserved before the read, are
> exactly what the PUT sends; multipart transfers keep the second validated
> read (see "Single-PUT payloads" below).
>
> **Amended in part by [#703](https://github.com/PaoloDelCasale/frostvault/issues/703)**
> (#700): from 8 MiB, a fresh upload whose Local Copy fingerprint carries the
> part digests the scan recorded builds its snapshot from the catalog; the
> validated send is then its only source read (see "Multipart snapshots from
> the scan's part digests" below).
>
> **Superseded in part by [ADR-0040](0040-ci-keeps-gates-not-descriptive-benchmarks.md)**
> (#636): CI no longer measures against v0.5.3, and the full stress sizes shrink
> to 256/512 with unchanged thresholds and publication gates.

Issue [#562](https://github.com/PaoloDelCasale/frostvault/issues/562). Small-file uploads pay fixed orchestration costs beyond their PUT. The Owner approved restructuring execution behind a deep UploadExecution Module, retaining one independently recoverable S3 key per Vault File, rather than adopting packs or relaxing integrity. This records the design contract and its implemented bounds; verified revisions and measured benefits are tracked in the issue, not inferred from the architecture alone.

## Decision

The Module owns preparation, send, proof and completion ordering for a claimed Job. Internal capacities and buffers are bounded, without increasing the installation's admitted in-flight Job limit or preclaiming work across a Paused Vault. Work on different Upload Snapshots may overlap; authority and durable outcomes remain per file. Pause still applies at the claim boundary (ADR-0033), not at every internal stage.

Consolidate adjacent replayable DB work, reuse process-local HTTP clients with distinct read/one-shot mutator retry policies, and deduplicate only positively committed facts. Keep indispensable durability boundaries: intent and send-started before sending; raw receipt journal immediately after the response, independently of cancellation or publication; verified replacement before deleting predecessors; completed only after cleanup. DB retries never replay provider or filesystem effects. A committed receipt is a fact, not publication authority (ADR-0024).

An initial stable namespace observation may supply both the upload intent and replacement inventory only when it was freshly obtained for the same attempt, provider, bucket, path and lifecycle-fenced target. Historical pins are not fresh observations and are never rebased to enable this shortcut. Resume, ambiguity, Path History ownership, external writers and exact-version deletion retain their fail-closed contracts. Ordinary HEAD contradictions and requested Read-back remain enforced.

## Implemented resource ownership

`UploadExecution.run(claimed_job)` borrows synchronous stage permits from the existing foreground Job threads, with no stage executor, payload/receipt queue or extra preclaims. Process-local stage limits derive from the deployment's immutable `operation_concurrency` c (1..32): `min(c, 8)` preparation readers (2 MiB read-chunk budget per reader), c sends, and c nonurgent catalog units with at most `max(2, ceil(c / 2))` per Vault; every count is capped by c, so no stage has more permits than claimed Jobs. c=16 gives 8/16/16/8, c=4 gives 4/4/4/2. Per-Vault admission precedes global catalog admission, both before connection checkout or Vault locks. DB retry sleeps retain no permit. Completion-driven refill still uses the ordinary fair scheduler and claim boundary.

Send buffers are admitted **before** intent/send-started and materialization (a single-PUT payload: before its source read, #702). Ordinary payload capacity is 64 MiB. One separate oversized multipart allocation uses the same send-count permits and is bounded by `2 * (MAX_PART_SIZE + CHUNK_SIZE)` (currently 2 GiB + 2 MiB), without blocking small buffers behind an impossible weight. This is a payload budget, **not an RSS limit**; Python/SDK/TLS overhead and bounded snapshot metadata are additional. Send permits last through joined I/O and the immediate journal, then are released before publication/cleanup. LIST/HEAD/Read-back/recovery retain their existing claimed-Job/streaming bounds, not the global send-count limit.

Autonomous receipt appends bypass catalog admission. A bounded process-local latch of failed receipt identities initially closes new sends; it retains no response payload and does not replace durable restart reconciliation. **#569 refines this gate:** a committed, empty INSERT into the receipt journal proves that its write path is reachable again and releases only the failure identities observed before the probe. It does not confirm any lost receipt, clear `send_started`, change the token/snapshot, or grant a second send to that intent. Durable ambiguous intents remain reconcile-only, even in disabled, paused or unavailable Vaults; they no longer block unrelated uploads. A failed probe/commit keeps new sends held. Jobs waiting for this gate release their claims with a timed retry and unchanged transfer retry count, and health reports the hold without turning it into a failed transfer.

Cancellation still forbids ordinary resume and never grants PUT, multipart-abort, publication, cleanup or Job-completion authority. The former facts-only repair of cancelled intents is no longer part of unrelated send admission: it existed to reopen the global latch, and its S3 reads could themselves hold other Vaults behind an unavailable namespace. The probe performs no provider I/O and invents no replacement receipt; cancelled uncertainty remains durable without reactivating that Job. A generation-fenced snapshot preserves concurrent late failures, including the same fact failing again, until the next successful committed probe. Hashing and permit waits inspect local cancel/shutdown/loss every iteration and amortize remote renewal to one second; durable checks still fence effects. No new cancellation-latency guarantee is made for the existing final Local Copy rehash.

Snapshots of 8 MiB and more keep a **second validated source read** (the only one when the snapshot comes from the scan's part digests, #703): the transport reopens the file and validates each bounded part buffer against the immutable snapshot before sending it, rather than capturing payload between stages. Small SHA256 PUTs use a shared bounded transport executor; legacy multipart/read-back behavior remains. Process/configuration/retry-policy-scoped clients have distinct normal-read/part and one-shot mutator policies, fork reset and shutdown hooks. No new UI tuning, durable format, migration, packing or Lifecycle behavior is introduced.

### Single-PUT payloads (#702)

A snapshot below `MULTIPART_THRESHOLD` (8 MiB) is read **once** per upload; this replaces the second source read for those files only.

- **Admission order.** Before the read, the run reserves the payload on the ordinary buffer lane with the same weight as before, `size + 2 * CHUNK_SIZE`, sized from the stat already observed (a file of another size is refused before it is opened). The preparation reader permit is then held for the read only. The send-count permit, with its send-admission check, is still taken only around the send. The order stays buffer lane, then reader, then send count: no Job waits for a buffer while it holds a reader or a send-count permit.
- **One stable pass.** The pass keeps every check of the snapshot read: a regular file, `O_NOFOLLOW|O_NONBLOCK`, the stat and fstat fingerprint before and after, an exact EOF and the cancellation check at every chunk. It hashes the bytes and keeps them, and they become available only after a complete, stable pass. The Upload Snapshot has its usual shape: `FULL_OBJECT`, `part_checksums=[digest]`, mtime and ownership from the stat. The automatic gate, the match with Current, cleanup, intent and send-started follow as before.
- **The bytes sent are the bytes hashed.** The PUT sends exactly that buffer without reopening the file, after an in-memory SHA-256 check of the buffer against the snapshot. The transport keeps its size check of the path before the send.
- **"B is never uploaded with A's evidence" still holds.** The evidence is computed from the very bytes sent. A change during the read fails the pass, and nothing is kept or sent. A later change cannot alter the buffer, so the PUT can only send A under A's evidence. The newer observation has its own generation and its own upload, and publication still compares against the pre-hash generation.
- **Lifetime.** The buffer and its reservation belong to the claimed run only, never to the catalog, the Upload Snapshot or any structure shared between Jobs. They live through the PUT and the immediate receipt journal. They are released on every exit: a match with Current, the automatic gate, cancellation, an error or any `BaseException`.
- **Memory.** The ordinary payload budget stays 64 MiB. A small payload now occupies it from before the read rather than only around the send, so at most six payloads near 8 MiB (about 10 MiB of weight each) are hashed or sent at once; smaller files share the budget up to the buffer lane count. This remains a payload budget, not an RSS limit.

Files of 8 MiB and more, and the transport callers without a snapshot (rename, copy, recovery), are unchanged.

### Multipart snapshots from the scan's part digests (#703)

The scan verification records, with the digest of a Local Copy of 8 MiB or more, the SHA-256 of each of its multipart parts, cut with the snapshot's part size (ADR-0045, migration 0004).

- **No preparation read.** An upload Job with no linked version and no bound snapshot, whose observation (generation, size, mtime) carries that digest and its parts and whose digest is not Current's, takes the Upload Snapshot from them, with mtime and ownership from the stat it observed. It takes no preparation reader permit and opens nothing; the send-count permit and the large multipart buffer are admitted as before.
- **The validated send is the only read.** The transport checks each part buffer against the snapshot before sending it, unchanged: the bytes sent are still exactly the snapshot's bytes, so a stale fingerprint can never upload B under A's evidence.
- **A contradiction retires the fingerprint.** A part that does not match fails the send as a source change after its send-start; the fingerprint of that observation is retired by compare-and-swap, and the retry reconciles the started send, rehashes the whole file and follows ADR-0030 (see ADR-0045). A Job bound to a snapshot, a fingerprint without parts and files below 8 MiB hash as before.

## Consequences

Correctness is tested at the agreed execution, transport and observable catalog Seams, including claim loss, late acknowledgment, crash/transaction abort and Local Copy A becoming B. Measure completed-after-cleanup throughput and work per file against the fixed v0.5.3 baseline on the same isolated workload; label synthetic evidence as such. Keep existing CI stress sizes, thresholds and publication gates. No production benchmark or new release/deployment is implied.

Cross-file DB micro-batching is not implemented. Same-file DB-only consolidation already removes duplicate work while preserving immediate receipt and terminal boundaries; the remaining required stages do not by themselves justify a shared rollback/writer-starvation risk. Reconsider batching only with measurements demonstrating additional useful amortization without receipt delay, writer starvation or a broader retry blast radius. Do not add a queue merely to rename existing concurrency. Packing, a filesystem receipt journal, generation-fence migrations and removal of ordinary proof observations are separate decisions, not hidden performance optimizations in this restructuring.
