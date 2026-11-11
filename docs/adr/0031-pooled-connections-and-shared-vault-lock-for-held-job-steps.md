# Pooled connections and a shared Vault lock for held Job steps

Issue #535. Uploading many small files was slow for two reasons that did not
depend on the provider. Every `db()` opened a new PostgreSQL connection (about 65
per uploaded file), and every Job status write locked the Vault row
`FOR NO KEY UPDATE`, so all workers of one Vault queued on that row.

## Decision

### Connection pool

`db()` hands out connections from a per-process `psycopg_pool.ConnectionPool`. Its
size is `2 × OPERATION_CONCURRENCY + 8`: two handles per operation slot, because a
unit can open while an outer handle is still open, plus headroom for the API,
watcher, scanner and maintenance loops. It starts empty, grows on demand, and
closes surplus connections after 60 s idle. A checkout behaves like a new
connection:

- `with db() as connection` still yields the psycopg connection, which commits or
  rolls back exactly as before. The connection then goes back to the pool instead
  of closing. `owned_connection` keeps working: the second close of a checkout
  does nothing, so a stale handle can never return a connection that another
  caller now holds.
- A returned connection is rolled back if needed and reset with `DISCARD ALL`.
  Session settings go back to the startup options, and prepared statements,
  temporary tables, session advisory locks and LISTENs are dropped. Autocommit,
  isolation, read-only, deferrable and the `dict_row` row factory are restored.
  Attributes a caller set on the connection object during the checkout (the
  directory-aggregate tracker) are removed.
- An idle connection is checked before it is handed out, because it can die
  while idle and a new connection could not.
- When every pooled connection is in use, or none arrives within 1 s (for
  example while the server is down), the caller gets a direct connection, as
  before #535. Pool-specific waiting is bounded, and pool saturation alone
  cannot reject a caller that can still open a direct connection. Nested handles
  therefore cannot deadlock solely on pool capacity.
- A forked child forgets the parent's pools without closing their sockets. The
  application closes its pools after the background workers drain at shutdown,
  and at interpreter exit. Opening a pool for a new DSN (test databases) closes
  the pools of other DSNs.

### Vault row lock

A worker's own step between two held statuses (`downloading`, `uploading`,
`verifying`, `cleaning`) takes the Vault row `FOR KEY SHARE` instead of
`FOR NO KEY UPDATE`. This applies only while the worker holds a claim, has no
Upload Remediation suspension or closing reason, and the Job is not bound to an
Upload Remediation episode. Such a step writes only:

- its own Job row, through a compare-and-swap on the claim token;
- append-only directory-aggregate dirty marks. Their primary key includes the
  writer's transaction identity, and maintenance consumes only marks that its
  snapshot sees.

No `directory_aggregates` or `directory_aggregate_status` row is written, so
`lock_vaults_in_order` gains no new writer. The Job is non-terminal before and
after, so admission predicates (`status NOT IN` terminal) keep their answer. The
first step moves a claimed Job from `queued` to a held status. Readers of
unstarted work either skip Jobs with a live claim (the scheduler's runnable
predicate), or lock the Vault `FOR UPDATE` (cancel-pending; Stop Protection and
deletions also do). `FOR UPDATE` still conflicts with `KEY SHARE`, so those paths
stay serialized with these steps. Bindings to
remediation episodes are written only when the Job is created or completed, never
while a claimant holds it, so the unlocked check for a binding cannot go stale.

`KEY SHARE` is still taken first, before the Job row. A step that took no Vault
lock would lock its Job row first, then ask for `KEY SHARE` on the Vault through
the dirty-mark foreign key. That order deadlocks with cancel-pending, which holds
the Vault `FOR UPDATE` and then updates the Job. `KEY SHARE` conflicts with a
subset of what `NO KEY UPDATE` conflicts with, in the same position. So the change
adds no wait-for edge, and no new deadlock. #485 came from an inverted lock order.

These keep `FOR NO KEY UPDATE` (or stronger):

- every move to a queued, terminal or retrying status;
- steps of bound remediation Jobs, and steps with a suspension or closing reason;
- `schedule_upload_retry`. It releases the lease and returns the Job to the
  unstarted set that cancel-pending and the scheduler select. Sharing the lock
  there was not proven safe, and retries are rare;
- `upload_provenance.target` by default, including receipt/version publication
  and `persist_verification` (Local Copy observation touches the Vault through
  `_get_or_create_file`). The audited exceptions below do not change publication
  or directory-aggregate writer authority (ADR-0021, ADR-0023, ADR-0024);
- every admission, result publication and directory-aggregate writer.

### Audited upload catalog units (#551)

`target`, `require_target`, `prepare`, `mark_send_started` and
`_upload_catalog_unit` accept explicit `shared=True`, using **FOR SHARE**, not
KEY SHARE. This still conflicts with lifecycle/identity changes and admission's
`UPDATE vaults SET name=name` (NO KEY UPDATE), so their checks remain fenced
until commit; shared upload units on the same Vault can overlap. Pause stays at
the claim boundary (ADR-0033): an already leased upload still finishes.

Opted-in DB-only units are intent read/resume binding plus Job target read;
resume target checks and exact-version linkage; snapshot/digest persistence and
reusable-version lookup (including lineage/receipt reads); verified reuse
linkage plus target read; unverified reuse linkage; pre-send target pinning;
intent preparation and send-started (including discovery fact capture);
verification-method/proof/egress metadata; and the admitted rename digest.
Nested target checks explicitly retain SHARE. No such unit creates file
identity, writes catalog events/aggregate rows/status/rebuild requests, or calls
`set_job`; those would upgrade the Vault lock and can deadlock with another
shared holder. Dirty aggregate invalidations retain their durable append-only
contract. Exclusive remains the default for all unaudited callbacks.

Consecutive intent/target reads and verified reuse link/target reads share one
transaction. No unit crosses provider I/O: send-started commits **before** send,
and post-provider DB retries retain the exact VersionId without another PUT.
SQL-recording regressions audit complete shared callbacks, PostgreSQL tests
prove overlap and fencing against exclusive/admission/lifecycle writers, and
the unchanged 2048-file/16-worker burst also bounds pause/cancel/resume writer
latency. Stress profiles, throughput floors and publication gates are unchanged.

### Snapshot cleanup read units and the claim checkpoint

Five more DB-only units of one upload take the Vault row `FOR SHARE`. They are
the namespace pin read and the keeper pin read of `snapshot_cleanup`, the unit
that checks the pin before the provider listing, and the two units that save
the listed inventory and bind the keeper. Each one reads the target, extends
the Job's own lease and, for the last two, writes the
`snapshot_cleanup_inventories` row of this upload (one active Job per Vault
File; the insert and the keeper binding are conditional on that row, so a
replay cannot overwrite another observation). Lifecycle, identity and
admission writers still conflict with `SHARE`. None creates file identity, publishes a
version, writes catalog events or aggregate rows, or calls `set_job`, so none
can ask for a stronger Vault lock. The units that record proof, the
destructive delete, the purge result and the terminal status keep
`FOR NO KEY UPDATE`. One upload now takes five exclusive Vault locks instead
of ten.

A foreground checkpoint (`ensure_job_active`) still asks PostgreSQL at every
call whether the claim is live, with the predicate of the renewal. It extends
the lease only when its last extension is older than
`CLAIM_CHECKPOINT_RENEW_SECONDS` (15 s). The keeper thread still renews every
third of the lease, and `ensure_job_claim_owned_in_transaction` still renews
inside every catalog unit. Cancellation, takeover and expiry are observed at
the same checkpoints as before; a small upload stops writing about twenty
lease extensions, each of which was a non-HOT `jobs` update with its own
commit.

`tests.test_small_upload_cost` pins both numbers, audits that a transaction
holding the shared lock never asks for a stronger one, and requires every
statement of an upload to be servable by an index on the tables that grow with
history (migration 0072).

## Consequences

- A burst of small uploads reuses a handful of server sessions instead of opening
  one per database step. Workers of the same Vault no longer queue on its row for
  their intermediate steps. #535 records the measured before and after numbers.
- `tests.test_aggregate_deadlocks` runs a 2048-file burst with 16 workers and
  concurrent aggregate maintenance. It asserts a conservative 0.5 files/s floor
  and at most 200 new PostgreSQL connections. On the local isolated PG16 server,
  a 512-file comparison went from 2.0 files/s and 33,277 connections to 3.5 files/s
  and 17 connections; the full 2048-file run achieved 2.9 files/s with 17
  connections. The floor guards severe slowdowns, while the connection bound
  deterministically catches per-use connection churn; timing alone cannot
  distinguish pool/lock regressions on differently sized CI runners.
- A process keeps at most `2 × OPERATION_CONCURRENCY + 8` idle connections open.
  `max_connections` must allow that, plus direct overflow connections under
  saturation.
- No migration and no functional change. The S3 round trips per object are not
  changed (ADR-0026, "Not decided here").
