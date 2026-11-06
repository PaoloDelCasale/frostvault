# Proven absence closes an ambiguous send

Issues #484 and #486 show a send that never reached S3 (database and network
lost together, a dependency lost between the committed send-started record and
the request) stuck in `ambiguous_send` forever, with no action available.

## Supersedes (in part) ADR-0024

Only these statements of [ADR-0024](0024-upload-facts-and-proven-duplicate-cleanup.md)
are changed; ADR-0024 carries the matching "superseded in part" note:

1. *"Zero token matches is not absence proof."* Zero matches alone still is
   not; zero matches **plus** the positive S3 proof below is.
2. *"Explicit reconcile-only admission ... cannot PUT."* A reconcile-only
   attempt whose send is proven absent stops being reconcile-only: it is the
   file's ordinary upload for that exact intent and may send. Admission itself
   (what the sweep and Retry failed uploads may create) is unchanged and still
   never creates a Job that can PUT.

Token/receipt rules, exact identity, cleanup and replacement authority, and
cancellation never resurrecting a target are unchanged.

## Decision

A committed send-started record still forbids another PUT. It may be closed as
a definitive no-write outcome only when S3 itself proves the send cannot have
landed, all of:

- two complete, stable version listings equal to the inventory pinned before the
  send (same versions and delete markers; no version carries the token);
- no incomplete multipart upload exists for the exact key, and a later complete
  listing still equals the pinned inventory, so a request finishing meanwhile is
  seen;
- no receipt exists for the intent, and the Job is the file's only active
  operation (the target gate already enforces this).

Any read failure, unsupported or unconfirmed multipart listing, delta, marker or
receipt keeps the block. A refused listing (`AccessDenied` on `ListBucketVersions`
or `ListBucketMultipartUploads`) is a per-file `list_permission_denied` block,
never an installation readiness failure; credential errors still suspend through
the readiness gate, and network or 5xx errors retry the Job.

The outcome is journaled as the allowlisted immutable fact
`NoWriteOperation: ProvenAbsent` and `send_started` is cleared, the same journal
used for an explicit auth refusal. The Job then sends from the unchanged
intent, token and frozen snapshot. The remediation episode is **not** resolved
at that point: it stays open and linked to the Job and resolves, with the
original failure notification, only when the upload completes through the
ordinary completion/cleanup postcondition. An intent whose send never started has
no provider obligation: it neither blocks nor binds later uploads and is re-pinned
or settled when the next upload prepares. Content that changed meanwhile fails the
Job as a source change, never as an identity change.

An explicit reconcile does not bypass paused protection: a proven absence on a
path with paused automatic protection stays blocked as `protection_paused` and
sends nothing until protection is resumed.

## Assumptions

- **Strong list consistency.** The proof reads S3 listings and treats two equal
  complete listings as the namespace. AWS S3 is strongly consistent for listings;
  an S3-compatible provider with eventually consistent listings (stale or partial
  `ListObjectVersions` / `ListMultipartUploads`) can prove absence wrongly. Such
  providers must not be relied on for this decision.
- A multipart orphan above the single-PUT threshold keeps `ambiguous_send` until
  its lifecycle abort (7 days) or an exact abort of its UploadId, tracked
  separately.

## Consequences

- Residual risk, accepted: a request from the dead sender that lands after the
  absence reads would create a second identical version carrying the same token.
  That is never data loss: the next reconcile sees two token matches and blocks
  with `ambiguous_receipt`, and cleanup can report `inventory_changed` or
  `orphan_different` (and a transient `identity_changed`) for the unknown
  version. That version is never in the set cleanup may delete, so nothing is
  removed on its account.
- Losing the PostgreSQL connection is retryable (`database_connection`), bounded
  by the upload retry budget. This is safe only because the send is journaled: a
  retry reconciles, or sends after proven absence, never repeating a blind PUT.
- `identity_changed` means an observed change of path, Vault or Source Volume
  root. Active operations, suspended Vaults/volumes, cancellation, resolved
  episodes and cloud-only conflicts have their own reasons; an unreadable root or
  a suspended Vault makes the Job wait queued and resume by itself.
