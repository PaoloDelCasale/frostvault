# One Upload Snapshot, one token per send attempt

Status: accepted (issue #588, epic #578). Amends
[ADR-0025](0025-proven-absence-closes-an-ambiguous-send.md) and
[ADR-0030](0030-a-stale-upload-snapshot-closes-its-unsent-intent.md) only as
stated here; every other upload safety rule stands (ADR-0035).

The v0.5 upload intent held the snapshot, the target, the token, the pinned
inventory and the send state in one row, and the Job, the Archive Version and
the remediation each kept another copy of the snapshot or of its digest. A
proven absence cleared `send_started` and the same intent and token sent again;
the no-write outcome was written into the receipt journal as a fact.

## Decision

- The **Upload Snapshot** and its target (Vault, path epoch, Source Volume root,
  provider endpoint; the key is the Vault namespace key of that path) are
  stored once. Jobs, the Archive Version and remediation reference it; none of
  them copies the snapshot, the target or the expected digest. A verified
  version's digest is the one verification proved.
- Each send is an **Upload Attempt** of that snapshot with its own token and
  the inventory pinned just before it. Its send-start is committed before the
  request, is never cleared, and forbids sending that attempt again.
- A definitive no-write outcome (an auth refusal joined by the transport, or
  S3's proof of absence) **settles** the attempt as `no_write`; it is not a
  receipt and erases nothing. Sending again opens a new attempt, with a new
  token, on the same snapshot. ADR-0025's "sends from the unchanged intent,
  token" is replaced by this; its proof of absence is unchanged.
- **Receipts** belong to the attempt only, never to a Job: they are journaled
  on their own connection whatever the attempt's state, and survive
  cancellation, rollback, commit errors and collection of the Job. A receipt
  that contradicts the settlement (any receipt of an unsent or no-write
  attempt, another VersionId than the landed one) blocks reconciliation and
  new snapshots of the file, however late it arrives.
- A stale snapshot (ADR-0030) is **closed**, never rewritten: its unsent attempt
  is settled `unsent` and the Job forgets it. The next attempt takes a new
  snapshot.
- Collection never takes open uncertainty: an unsettled attempt, an open
  multipart upload, a contradicting receipt or an Archive Version referencing
  the attempt keep it, whatever its `settled_at`. Snapshots, attempts,
  receipts and multipart rows are immutable apart from these one-way
  transitions.

## Consequences

- A request of a dead sender that lands after a proven absence carries the
  settled attempt's token, never the new one: the new attempt's reconciliation
  sees an unknown version (`inventory_changed`) instead of two matches of its
  own token. Its late receipt, if any, blocks as a contradiction.
- Several attempts of one snapshot are history of the same content; the cost of
  a small upload does not depend on them (one open attempt per file, one landed
  attempt per snapshot).
