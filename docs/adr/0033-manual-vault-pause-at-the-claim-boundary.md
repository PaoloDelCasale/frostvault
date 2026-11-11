# A manual Vault pause holds new cloud Jobs at the claim boundary

Issue #537 implements the Paused Vault in CONTEXT: an indefinite Owner hold, not
Stop Protection, a Source Volume suspension, or an operating window. A separate
persistent `vaults.paused` boolean defaults to false (migration 0070). No Job,
Local Copy, Archive Version, root allocation or Storage Plan is changed by pause
or resume. Restart preserves the hold; resume simply makes existing Jobs eligible.

## Scope

Pause holds newly claimed uploads, renames, free-space, storage-class moves and
ordinary cloud purges, regardless of scheduling tier or manual/automatic origin.
Owner requests may still be admitted, but queue behind the hold; requesting an
upload is not an implicit resume. This makes a bandwidth/maintenance hold useful
even with Owner uploads already queued. Recoveries are explicitly exempt at every
stage (queued, approved, restoring and download). A storage-class Job already
`restoring` is also exempt so its paid temporary cloud copy is consumed before
expiry. Unanswered recovery approvals still expire under ADR-0032.

Already leased Jobs finish, including the short interval between claim and handler
start. Pause never revokes/renews a lease or cancels work. A retry released back to
the queue is a new start and waits. Local scans/watchers and local-only admission
continue; a new automatic upload can be queued but cannot claim. Scan observation
(local and cloud inventory), shared bucket Storage Plan synchronization and
provider-managed lifecycle transitions are not transfer Jobs and continue; pause
is not an S3 outage switch or a promise of zero cloud API calls. Explicit Stop
Protection remains a separate lifecycle decision, including its already-consented
purge, and is not defeated by a previous pause. Existing source identity, lifecycle,
authentication, CSRF, reauthentication and exact-version S3 protections still apply.

## Serialization and visibility

Pause/resume lock the Vault `FOR UPDATE` before changing the flag. Claims take a
Vault-first `FOR KEY SHARE SKIP LOCKED` before the conditional Job update and
recheck the shared eligibility predicate in a fresh statement. Thus either a claim
commits first and runs to completion, or pause commits first and the Job stays
queued. A contended Vault cannot block service of another Vault. Concurrent claims
and ordinary held progress steps retain the shared locks of ADR-0031, never an
exclusive Vault lock on every progress update. Candidate windows and queue depth
use the same pause/recovery predicate before cutting fair scheduling windows.

Overview and settings show the separate pause badge and an explicit pause/resume
control. Each real transition records a localized inbox/optional email notice;
repeated POSTs are idempotent. No scheduled pause and no duplicate bulk-cancel API:
`POST /api/upload/cancel-pending` with empty path already exists (ADR-0026).
Downgrade refuses an active-lifecycle hold rather than silently restarting cloud
work. A Stop Protection tombstone's historical flag does not strand downgrade:
it cannot resume and has no ordinary cloud Jobs to restart.
