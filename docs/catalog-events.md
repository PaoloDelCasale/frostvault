# Catalog event stream (#227)

Authenticated browsers subscribe to `GET /api/catalog/events` (SSE) and may
call `GET /api/catalog/revision` for one-shot catch-up after focus/online.

## Source of truth

- Durable journal: `vault_catalog_revisions` + `catalog_events`
- In-process hub: wake-up only (never advances the client high-water mark alone)

## Per-stream tick cost

Default cadence: **2 seconds** (`DURABLE_POLL_SECONDS`), or sooner when the hub
wakes the wait.

Each tick opens **one short-lived DB connection** and runs at most:

1. Session + user + membership authorization (single JOIN)
2. Catalog high-water / retention markers
3. At most `MAX_CATCHUP_EVENTS + 1` journal rows (`64` by default)

No transaction is held across awaits. Backlogs larger than the bound emit one
`has_gap` / invalidate-all frame at the high-water revision instead of paging.

## Reviewer reproduction (clean worktree)

Local `.venv/` and `**/node_modules/` are gitignored. Use an external toolchain:

```bash
# Python (example path on this host)
export PY=/path/to/frostvault/.venv/bin/python
$PY -m unittest tests.test_catalog_events_http tests.test_catalog_state -v
$PY -m unittest discover -s tests -v   # full backend when desired

# Frontend
export npm_config_cache=/path/to/npm-cache
ln -sfn /path/to/frostvault/frontend/node_modules frontend/node_modules
(cd frontend && npm run test && npm run typecheck && npm run lint && npm run build)
rm -f frontend/node_modules
```
