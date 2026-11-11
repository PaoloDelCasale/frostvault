# Keep per-file upload evidence while restructuring execution

Issue [#562](https://github.com/PaoloDelCasale/frostvault/issues/562). Small-file uploads pay fixed orchestration costs beyond their PUT. The Owner approved restructuring execution behind a deep UploadExecution Module, retaining one independently recoverable S3 key per Vault File, rather than adopting packs or relaxing integrity. This records the agreed design contract; implementation and measured benefits are tracked in the issue and must not be inferred from this ADR alone.

## Decision

The Module owns preparation, send, proof and completion ordering for a claimed Job. Internal capacities and buffers are bounded, without increasing the installation's admitted in-flight Job limit or preclaiming work across a Paused Vault. Work on different Upload Snapshots may overlap; authority and durable outcomes remain per file. Pause still applies at the claim boundary (ADR-0033), not at every internal stage.

Consolidate adjacent replayable DB work, reuse process-local HTTP clients with distinct read/one-shot mutator retry policies, and deduplicate only positively committed facts. Keep indispensable durability boundaries: intent and send-started before sending; raw receipt journal immediately after the response, independently of cancellation or publication; verified replacement before deleting predecessors; completed only after cleanup. DB retries never replay provider or filesystem effects. A committed receipt is a fact, not publication authority (ADR-0024).

An initial stable namespace observation may supply both the upload intent and replacement inventory only when it was freshly obtained for the same attempt, provider, bucket, path and lifecycle-fenced target. Historical pins are not fresh observations and are never rebased to enable this shortcut. Resume, ambiguity, Path History ownership, external writers and exact-version deletion retain their fail-closed contracts. Ordinary HEAD contradictions and requested Read-back remain enforced.

## Consequences

Correctness is tested at the agreed execution, transport and observable catalog Seams, including claim loss, late acknowledgment, crash/transaction abort and Local Copy A becoming B. Measure completed-after-cleanup throughput and work per file against the fixed v0.5.3 baseline on the same isolated workload; label synthetic evidence as such. Keep existing CI stress sizes, thresholds and publication gates. No production benchmark or new release/deployment is implied.

Micro-batching DB stages is conditional on measurements demonstrating useful amortization without receipt delay, writer starvation or a broader retry blast radius. Do not add a queue merely to rename existing concurrency. Packing, a filesystem receipt journal, generation-fence migrations and removal of ordinary proof observations are separate decisions, not hidden performance optimizations in this restructuring.
