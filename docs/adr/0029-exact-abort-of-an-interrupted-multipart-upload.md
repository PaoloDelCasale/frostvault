# Exact abort of an interrupted multipart upload

Issue #491. [ADR-0025](0025-proven-absence-closes-an-ambiguous-send.md) refuses to
prove absence while an incomplete multipart upload exists for the key, because it
could be the dead sender's. A multipart upload that crashed after
`CreateMultipartUpload` therefore kept the file in `ambiguous_send` until the
bucket lifecycle reaped it (7 days).

## Decision

The upload facade journals every `UploadId` the intent's own
`CreateMultipartUpload` returned, **before** s3transfer receives it (so before any
part): table `upload_multiparts(intent_id, upload_id, state open|closed)`,
migration `0066_upload_multipart_journal`. If the id cannot be journaled, the
just-created upload is aborted and the upload fails; it never continues
untraceable. The row is closed when the upload completes or the facade's abort
succeeds (or answers `NoSuchUpload`). A failure to close only leaves the row open.
Rows are also closed when reconciliation publishes the version it found by token (a
Complete that reached S3 but whose response was lost), when the proof of absence
closes the intent, and when the intent is settled. A closed row means "no live
obligation", not "the provider is clean".

When the proof of absence meets incomplete uploads for the exact key, it calls
`AbortMultipartUpload` **only for the ids still open in this intent's journal**,
lists again, and keeps the block unless nothing remains. The existing conditions
are unchanged and all must still hold: no receipt, two equal complete version
listings equal to the pinned inventory (no version outside it, no token carrier),
and a final equal listing. Once absence is proven every open row of the intent is
closed in the same database unit as the no-write outcome.

Never aborted: an upload not in the journal (another writer, an older release, the
unjournaled window between the provider's answer and the journal commit), an upload
of another key, anything while a version outside the pinned inventory exists for the
key (the multipart branch is then not reached). They keep `ambiguous_send`, exactly
as before.

A refused abort is a per-file `abort_permission_denied` block through the same fresh
bucket probe as every other S3 action (ADR-0025, #492): probe failing -> readiness
gate; passing -> the file is blocked and the gate is untouched. Its remedy says that
the incomplete parts are still stored (unlike `write_permission_denied`, where
nothing was). The journaled row stays open.

## Alternatives considered

- *`ListMultipartUploads` and match by key, initiator and time, token in metadata.*
  The listing returns neither the object metadata nor a trustworthy initiator for
  every provider, and a time window is a guess; the token cannot be read from an
  incomplete upload. Rejected: it cannot prove ownership.
- *Abort every incomplete upload for the key.* Could destroy a concurrent writer's
  upload. Rejected.

## Consequences

- Needs `s3:AbortMultipartUpload` on the bucket objects, which the documented
  operator policy and the Terraform module already grant for SDK cleanup.
- Residual: a crash in the instant between S3's answer and the journal commit leaves
  an upload nobody can trace; it stays `ambiguous_send` until the 7-day lifecycle.
  The same holds when `CreateMultipartUpload` itself succeeds at S3 but its response
  is lost: the UploadId is unknown to the application, so it cannot be journaled or
  aborted.
- Downgrade of the migration is refused only while an open row belongs to an intent
  that still owes reconciliation (send started, not settled): the journal is then
  the only proof that allows an exact abort. Rows of settled or never-sent intents
  are dropped with the table.
