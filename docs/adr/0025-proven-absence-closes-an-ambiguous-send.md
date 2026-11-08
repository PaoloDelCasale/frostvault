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
receipt keeps the block. A refused listing (any HTTP 403 on `ListBucketVersions`
or `ListBucketMultipartUploads` that is not a credential error) first runs a
**fresh bucket readiness probe**, the gate's own check:

- probe fails: the refusal is installation-wide (permissions revoked), so the
  error goes to the readiness gate and the Job stays queued, exactly as on any
  other S3 call, instead of failing in mass until the gate notices;
- probe passes: it is a per-file `list_permission_denied` block that never
  touches the gate (the probe does not exercise these two actions, so reopening
  the gate would only re-claim the Job forever).

Credential errors always go to the gate, and network or 5xx errors retry the Job.

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

A reconcile-only attempt (remediation or explicit reconcile) does not bypass
paused protection: a proven absence on a path with paused automatic protection
stays blocked as `protection_paused` and sends nothing until protection is
resumed. An ordinary upload, a manual one included, is the user's own send and is
never gated by it.

An episode whose re-armed send never started (the attempt failed, was cancelled
or the content changed before `send_started`) is *void*: it admits no Job, never
swallows ordinary uploads, never offers Resume, and is excluded from the bounded
retry and sweep queues so it cannot starve live episodes. The ordinary upload that
later protects the file resolves it, and its original failure notifications, once
that upload has completed with its exact cleanup postcondition.

## Follow-up (#492)

The same rule now covers every S3 action of an upload, not only the two listings.
A non-credential HTTP 403 on `HeadObject`/`GetObject` of an exact version or on
`PutObject`/multipart requests first runs the fresh bucket probe: failing -> the
readiness gate (the Job stays queued); passing -> a per-file block named after the
refused action (`read_permission_denied`, `write_permission_denied`; deletes keep
`cleanup_unavailable`) that never touches the gate. A refused write stored nothing
(journaled as a no-write outcome) and leaves no reconciliation block: the Job
fails with the missing permission and the ordinary retry applies once fixed. The
reason follows the botocore operation name, never the message.

Throttling (429, `SlowDown`), 408/400 `RequestTimeout`, 5xx, transport faults and
answers that are not a well-formed S3 response are "provider unreachable" for
the proof of absence and for the Job: retried with the existing bounded backoff,
never `inventory_changed`.

An unreadable Source Volume root waits without spending the retry budget: the Job
is rescheduled with exponential backoff (2 s doubling to 300 s) and its wait start
is kept in its own message parameters. At the first check after 10 minutes of
outage (about 14 minutes with that backoff) one in-app problem per Vault with the
reason `source_volume_unavailable` is shown. It resolves when a Job of the Vault
reads the root again, and in every scheduler cycle when the Vault has no upload
left waiting for the volume (cancelled, closed) or its root is readable again.
Only uploads are rescheduled this way; other actions keep their plain queued wait.

A 403 `InvalidObjectState` (a version in cold storage that needs an explicit
restore) is not a permission refusal and not a readiness error: the exact-GET
duplicate proof stays `proof_unavailable`, as before #492, and the gate is never
involved.

## Assumptions

- **Strong list consistency.** The proof reads S3 listings and treats two equal
  complete listings as the namespace. AWS S3 is strongly consistent for listings;
  an S3-compatible provider with eventually consistent listings (stale or partial
  `ListObjectVersions` / `ListMultipartUploads`) can prove absence wrongly. Such
  providers must not be relied on for this decision.
- A multipart orphan above the single-PUT threshold keeps `ambiguous_send` until
  its lifecycle abort (7 days) or the exact abort of its journaled UploadId
  ([ADR-0029](0029-exact-abort-of-an-interrupted-multipart-upload.md)).

## Consequences

- Residual risk, accepted: a request from the dead sender that lands after the
  absence reads creates a second version V1 with the pinned token. It is never
  data loss, because V1 is not in the set cleanup may delete. What it can cause
  depends on who sends next: a retry that reuses the token meets two token matches
  and blocks with `ambiguous_receipt`; but `prepare` may renew the token of an
  unsent intent (new snapshot, inventory), in which case V1 is just an unknown
  version and cleanup reports `inventory_changed` or `orphan_different`, or a
  transient `identity_changed`, instead.
- Losing the PostgreSQL connection is retryable (`database_connection`), bounded
  by the upload retry budget. This is safe only because the send is journaled: a
  retry reconciles, or sends after proven absence, never repeating a blind PUT.
- `identity_changed` means an observed change of path, Vault or Source Volume
  root. Active operations, suspended Vaults/volumes, cancellation, resolved
  episodes and cloud-only conflicts have their own reasons; an unreadable root or
  a suspended Vault makes the Job wait queued and resume by itself.
