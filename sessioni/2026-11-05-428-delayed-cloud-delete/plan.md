# #428 implementation

Base: origin/main 836933b34f1fd565ce3174b218393682aa94a631.

Read issue #428, parent #410, native dependency #422 (closed), blocking #430,
map #360 and resolutions #365/#366; related #408/#411/#418/#421/#423–#427/#429.

- Fixed seven-day delay, never acceleratable. Type the exact Vault name;
  recent Owner reauthentication for requests that delete cloud content.
- Admit only Cloud-only files, with authoritative local-absence checks and exact
  non-null provider VersionIds captured once; no runtime expansion to new versions.
- Recheck source identity, actual local absence, catalog identity and pending
  work before provider deletion; reappearance or ambiguity preserves cloud bytes.
- Stop protection always retains local data. Keep cloud releases the root once
  application work is quiescent; delete uses the same Cloud-only-only gate/delay.
  A local cloud-backed file blocks the delete choice (choose keep instead).
- Stop+delete retains root allocation through the delay/execution. Cancellation
  means keep cloud, not resume protection; completed/partial work is never undone.
- Preserve tombstone/history and audit sensitive actions; retire old decommission,
  enablement and acceleration UI/endpoints, not the existing exact-version worker
  and durable scheduler leases.
- Legacy pending deletion must not inherit its shorter delay; migration cancels
  unstarted legacy deletions and refuses interrupted destructive work or unfinished
  decommissions. No migration performs filesystem/S3 operations.
- Deterministic targeted tests locally; PostgreSQL/S3/full verification in PR CI.

Production services/data, Compose, credentials and /boot are out of scope.
Coordinator reviews final diff and merges; this agent must not merge or close #428.
