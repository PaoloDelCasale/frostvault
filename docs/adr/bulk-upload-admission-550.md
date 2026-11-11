# Durable bounded admission for manual bulk uploads

Issue #550. A 202 response is not asynchronous if the request first creates every
Job while holding the Vault lock. Directory and whole-Vault upload requests now
persist an admission record, return 202, and produce Jobs in bounded DB-only
transactions. Single-file admission remains synchronous. This does not change
scheduling tiers (ADR-0026), provenance locking (ADR-0031), or upload authority
(ADR-0024/0025).

## Finite selection rather than a live, growing folder

A request freezes the highest existing `file_paths.id`, its scope, Owner, manual
origin, verification policy and enrolled Vault identity. Each batch traverses
immutable path identities up to that watermark, never OFFSET over shrinking
eligible files. Renamed/retired paths and new paths after acceptance are skipped,
not followed or added. Content eligibility is checked when each batch runs; this
is not an Upload Snapshot or a filesystem snapshot. Upload workers still pin
content and exact-send evidence at their existing boundary. The selected count
is an initial catalog-path count, not a promise that every path is uploadable.
Acceptance therefore reads only current path identities, without joining all
Vault Files: stale statistics after a large scan can otherwise choose a
quadratic nested loop before returning 202. Inactive file identities consume the
bounded path window too; only the existing admission arbiter decides whether
to produce a Job. Eligibility changes cannot turn one batch into an unbounded
search for 100 still-active files.

This trades a fully materialized per-file selection at HTTP acceptance (and its
unbounded transaction) for conservative exclusion of renamed paths. No arbitrary
path replacement inherits the old selection. A replayed request identifier returns
the same group, including after cancellation/completion; reuse for a different
scope is refused. Retrying without an identifier is a new request.

## Durability, cancellation and lifecycle

Cursor, counters and produced Jobs commit together. Multiple processes serialize
Vault first, request second. Crashes before commit produce neither progress nor
Jobs; restart resumes the persisted cursor. The producer selects at most eight
Vaults per tick and commits their last-attempt times separately before acquiring
any Vault lock. Least-recently attempted requests rotate even when every earlier
Vault is locked or its batch rolls back. Attempt accounting never changes
progress, and no request-row selection lock survives into a Vault-first batch.
Each transaction revalidates the Vault lifecycle and enrolled identity and delegates to the existing upload
remediation arbiter, preserving reconcile-only, exact-send and active-file fences.
The requested policy applies to new uploads; remediation retains the original
Upload Snapshot's proof mode, never replacing it with a later policy.

Group cancellation stops admission even before the first Job exists. Folder
cancel-pending persists exclusions for future batches without stopping unrelated
folders. Whole-Vault cancel-pending closes pending admissions. Already produced
Jobs retain the existing safe cancellation predicates; ambiguous sends remain
with their own Resume. Stop Protection and Reconnect treat pending admissions as
active work. Identity/lifecycle drift invalidates an admission, never silently
rebinding it. Paused Vaults can admit but cannot claim (ADR-0033).

Durable group status exposes selected/processed/admitted counts separately from
transferred bytes. The existing activity indicator shows active admission progress
without calling it upload completion. Migration is metadata-only; downgrade
refuses pending admissions rather than losing future work silently.
