# History retention

FrostVault keeps its operational history bounded (ADR-0035, #596). A
background collector runs every hour and deletes **database rows only**: it
never deletes a local file, a cloud object or a metadata backup.

| Setting | Default | What is collected |
|---|---|---|
| `JOB_RETENTION_DAYS` | 30 | Terminal Jobs (`finished_at`), and the closed history that follows them |
| `NOTIFICATION_RETENTION_DAYS` | 30 | Read notifications, resolved problems, delivered email-only notices |
| `AUDIT_RETENTION_DAYS` | 90 | Audit events |

The Job window also applies, once nothing references them, to:

- closed Upload Remediation episodes (never an open one);
- Job groups with no Job left (their Restore Approval or Delayed Cloud
  Deletion request), except the Stop Protection consent of a stopping Vault;
- settled upload and copy attempts whose Archive Version no longer exists (or
  never existed), with their receipts and closed multipart records;
- closed Upload Snapshots with no attempt and no Job left;
- the per-Vault catalog event journal: older events are dropped and
  `vault_catalog_revisions.retained_from_revision` advances, so a reader
  behind it sees an explicit retention gap and reloads;
- File rows left by a purge in an active Vault: no Current, no replacement,
  no local copy and no history attached.

Age alone never authorizes collection. The collector never deletes a Current,
a replacement in progress, an unsettled attempt, an attempt with an open
multipart upload or a contradicting receipt, an open problem, a notification
still waiting in the terminal outbox or with a pending email, or anything that
open work, an open cleanup batch or a kept row still references.

Each pass walks its table in keyset order in short transactions of at most 500
rows. Candidates are locked with `FOR UPDATE SKIP LOCKED` and deleted by a
statement that rechecks every condition after the lock, so a concurrent writer
that attaches to a candidate either keeps it or fails on the missing row.
