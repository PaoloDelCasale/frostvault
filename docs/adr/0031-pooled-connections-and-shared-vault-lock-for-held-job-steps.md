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
- `upload_provenance.target`. It fences the Vault identity and lifecycle checks
  against relocation, moves and decommissioning. It also anchors the Vault-first
  order of the catalog unit that publishes the upload's results and directory
  aggregates (ADR-0021, ADR-0023, ADR-0024);
- every admission, result publication and directory-aggregate writer.

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
