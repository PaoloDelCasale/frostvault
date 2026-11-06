# Proven absence closes an ambiguous send

Issues #484 and #486 show a send that never reached S3 (database and network
lost together, a dependency lost between the committed send-started record and
the request) stuck in `ambiguous_send` forever, with no action available. This
amends **only** the zero-match sentence of ADR-0024; token/receipt, exact
identity, cleanup and replacement authority are unchanged.

## Decision

A committed send-started record still forbids another PUT. It may be closed as a
definitive no-write outcome only when S3 itself proves the send cannot have
landed, all of:

- two complete, stable version listings equal to the inventory pinned before the
  send (same versions and delete markers; no version carries the token);
- no incomplete multipart upload exists for the exact key, and a later complete
  listing still equals the pinned inventory, so a request finishing meanwhile is
  seen;
- no receipt exists for the intent, and the Job is the file's only active
  operation (the target gate already enforces this).

Any read failure, unsupported or unconfirmed multipart listing, delta, marker or
receipt keeps the block. Authentication and connectivity errors while reading are
not a changed inventory: the first suspends the Job, the second retries it.

The outcome is journaled as the allowlisted immutable fact
`NoWriteOperation: ProvenAbsent` and `send_started` is cleared, the same journal
used for an explicit auth refusal. The Job then sends from the unchanged
intent, token and frozen snapshot. A reconcile-only remediation attempt becomes
that ordinary upload and its episode is resolved without a keeper; failed
originals stay as history. Content that changed meanwhile still needs a new
snapshot and is not covered here.

## Consequences

- Residual risk, accepted: a request from the dead sender that lands after both
  absence reads would create a second identical version carrying the same token.
  That is never data loss; the next reconcile sees two token matches and blocks
  with `ambiguous_receipt`.
- Losing the PostgreSQL connection is retryable (`database_connection`), bounded
  by the upload retry budget. This is safe only because the send is journaled: a
  retry reconciles, or sends after proven absence, never repeating a blind PUT.
- `identity_changed` means an observed change of path, Vault or Source Volume
  root. Active operations, suspended Vaults/volumes, cancellation and resolved
  episodes have their own reasons, and an unreadable root waits instead of
  failing the Job.
