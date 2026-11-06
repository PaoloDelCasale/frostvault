# Separate upload facts from replacement authority

Issue #464 closes the provider-response/catalog gap with a committed pre-write
intent and opaque upload token tied to the immutable Upload Snapshot from
ADR-0023. Raw receipts survive cancellation or lost leases as facts only: they
cannot publish a version or authorize cleanup. This deliberately trades an
unrecoverable orphan or blind repeat PUT for durable, visible reconcile-only
ambiguity when a provider outcome cannot be proved.

## Consequences

A committed send-started record forbids another PUT without a definitive no-write
outcome. Zero token matches is not absence proof; multiple matches, contradictory
proof, unstable/incomplete listings or observable external writers block. Token,
ETag, size, time and catalog membership never establish integrity. Exact receipt
or unique token discovery is checked against the independent frozen expected
proof; owner/path/Vault/source/claim fences still govern publication. SDK
PutObject/CreateMultipartUpload/CompleteMultipartUpload use request-specific
one-shot clients (`total_max_attempts=1`); reads and same-MPU parts keep their
usual retries. No DB retry contains provider or filesystem I/O.

Replacement cleanup for upload/scan/rename has one per-VersionId authority gate.
Ordinary superseded app writes need independent lineage. Historical unknowns
need a complete exact-VersionId GET, identity/length and SHA256 equal to an
independently verified, available, current keeper. A duplicate is catalogued in
a separate durable proof table, not elected via Archive Version numbering;
discovery remains unverified and cannot displace a verified Current Snapshot.
Committed exact proof is reused after retries, including delete-to-DB gaps.
Different, unreadable or ambiguous content is retained with a deduplicated
Problem and safe remedy; there is no arbitrary orphan-removal workflow.

Before each exact delete the complete pinned inventory, keeper, namespace owner,
current path, lifecycle, Source Volume/root, claim and cancellation are rechecked.
A different Vault File at an old path is not a cleanup target, even for identical
bytes. Delete Markers have no replacement consent and are retained. Delayed
Cloud Deletion keeps its existing explicit exact consent and seven-day delay.
The only verified keeper is never targeted. Completion still requires cleanup,
not merely Integrity verified (ADR-0016).

Application row locks do not serialize external S3/SMB writers or provide an
atomic listing+delete snapshot. Keep those writers quiescent. Every observable
race blocks; newly observed versions are never added to a pinned target set,
and earlier in-flight/completed deletes cannot be undone. Historical GETs incur
requests, possible cold retrieval and egress; exact cold versions require an
explicit external restore, never an automatic restore by reconciliation.

Migration 0064 is metadata-only, without synthesized tokens/trust or live I/O.
Downgrade refuses unresolved send-started facts and pending cleanup proofs before
DDL, including deliberately cancelled ambiguous sends. Explicit reconcile-only
admission is separate from normal upload eligibility and cannot PUT. Cancellation
retains the journal and cannot automatically resurrect the target. #459 owns
bounded automatic discovery/admission, remediation history and historical failure
resolution; this decision adds neither a sweep nor live orphan cleanup.
