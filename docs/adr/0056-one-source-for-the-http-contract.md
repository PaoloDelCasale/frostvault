# One source for the HTTP contract and one address for a Vault

Status: accepted by the Owner on 2026-11-23 (issue #805, epic #788).
Implemented: all five steps under "Migration" are done (steps 4 and 5
completed on 2026-11-24). The Context below describes the code before the
migration.

## Context

Counts are from `main` at `d7a846c5` (after #796, #797 and #800).

**The contract is written twice.** Requests are Pydantic models next to their
routes: 31 `BaseModel` classes in `app/routes/`. Responses are 98 schemas
written by hand in `app/openapi_schemas.json` (148,697 bytes).
`app/api_models.py::response_model(name)` builds an empty subclass of
`JsonObjectResponse` (`RootModel[dict[str, Any]]`): at runtime it only checks
that the route returned a JSON object. `app/main.py::_openapi_schema()` then
copies the hand-written schema of the same name over that open component. The
hand-written fields are never compared with what a route returns.

**What the copy hides today.**

- 9 hand-written schemas are referenced by no route and by no other schema:
  `CloudPurgePayload`, `FileOperationPayload`, `FileOperationResponse`,
  `FilesQuery`, `GeneralVaultReconnectPayload`, `JobCancelPayload`,
  `StopProtectionPayload`, `VaultCreateRequest`, `VaultSelectRequest`. Three
  have no user at all (`CloudPurgePayload`, `FilesQuery`, `JobCancelPayload`).
  The other six are imported by the frontend as types
  (`frontend/src/api/vault.ts`, `frontend/src/api/endpoints.ts`) for requests
  whose real contract is a Pydantic model with another name
  (`VaultSelfServiceCreate`, `VaultSelection`, `StopProtectionRequest`,
  `VaultReconnect`, `RecoverRequest`/`FileAction`).
- A tenth, `LifecycleProfileSelection`, exists in both places; the Pydantic
  one wins and the hand-written one is dead text.
- `GET /api/problems` declares `NotificationsResponse`, which requires
  `unread_count`, `email_configured` and `has_more`; the route returns only
  `items` and `open_problem_count`.
- 15 operations declare the bare `JsonObjectResponse`, so the frontend types
  their answers by hand: `POST /api/login`, `/api/logout`, `/api/reauth`,
  `/api/notifications/read`, `/api/notifications/read-all`,
  `/api/general/metadata-backups/run`, `/api/upload`,
  `/api/files/cloud-only-conflict/replace`, `/api/recover`,
  `/api/recover/approve`, `/api/free-space`, `/api/cloud-purge`, and
  `GET /api/general/metadata-backups`, `/health`, `/ready`.
- 41 of the 98 schemas are open (`additionalProperties: true`), 26 closed, the
  rest say nothing.

**The generated artifacts.** `frontend/openapi.json` (11,936 lines) is
committed; `npm run generate:api` turns it into
`frontend/src/api/openapi.generated.ts` and the aliases in
`frontend/src/api/types.ts` (#800). CI regenerates the two TypeScript files and
fails on drift, and `tests/test_openapi_contract.py` compares the committed
document with `app.openapi()` as parsed JSON. Nothing in CI writes
`openapi.json`: `scripts/export_openapi.py`, the command `docs/ci.md` names,
writes sorted keys while the committed file is in generation order, so running
it changes 18,348 lines (#829).

**A Vault is addressed in three ways.** Of the 99 documented operations:

- 22 carry the id in the path, `/api/vaults/{vault_id}/…`;
- 13, on 11 paths under `/api/vault/…`, use `current_vault`
  (`app/routes/deps.py`): the `?vault_id=` query parameter, or without it the
  Session's Vault, or without that the first operational Vault by name, which
  the dependency then writes into the Session;
- 24 more use the same query-or-Session addressing without `vault` in their
  path (browsing, Jobs, Uploads, recovery, the catalog event stream).

`VaultMoveRequest` also carries `vault_id` in its body, and `POST
/api/vault/move` answers 409 `api.vault_move.vault_changed` when the body and
the addressed Vault differ.

The frontend already names the Vault on every one of these calls
(`vaultApi(vaultId)` in `frontend/src/api/vault.ts`, ADR-0022): it never relies
on the Session's Vault for them.

## Decision

1. **The Pydantic models next to the routes are the only source of the
   contract**, for requests and for responses. `frontend/openapi.json` and the
   TypeScript types are generated from them. `app/openapi_schemas.json`
   disappears, and with it `response_model(name)`, `documented_schemas()` and
   the merge in `_openapi_schema()`.
2. **Responses are checked against their model only in tests.** Production
   keeps today's path: the route's dictionary passes through unfiltered and
   unvalidated, at the same cost. A response model is therefore declared
   through a pass-through that documents the model without making FastAPI
   validate or filter with it; the shared test client (`tests/fixtures.py`)
   validates every JSON success body against the declared model and fails on a
   missing field, a wrong type, or an undeclared field of a closed model.
3. **One address for a Vault: `/api/vaults/{vault_id}/…`.** All 37
   operations that take their Vault from the query or the Session move there:
   the 13 under `/api/vault/…` and the 24 others (the Owner extended the
   decision to these on 2026-11-23). The old addresses are removed, with no
   alias. `vault_id` leaves the body of `VaultMoveRequest`. The frontend
   changes in the same pull request as each group of routes.
4. **Errors keep the shape of #797**: `ApiErrorResponse` (`detail`, `code`,
   `message_key`, `message_params`), produced by the single handler from
   `app/errors.py`. It becomes a Pydantic model like every other schema; its
   fields and statuses are not reopened here.
5. **`scripts/export_openapi.py` is the only writer of
   `frontend/openapi.json`**, and it writes the order the application
   generates (no `sort_keys`), which is the committed order: the script and the
   file agree without a rewrite. CI runs it before `generate:api` and fails on
   any byte of drift.

### Addresses that change

| Method | Old address | New address |
|---|---|---|
| GET | `/api/vault/operation-policy` | `/api/vaults/{vault_id}/operation-policy` |
| PUT | `/api/vault/operation-policy` | `/api/vaults/{vault_id}/operation-policy` |
| POST | `/api/vault/operation-policy/globs` | `/api/vaults/{vault_id}/operation-policy/globs` |
| POST | `/api/vault/operation-policy/preview-globs` | `/api/vaults/{vault_id}/operation-policy/preview-globs` |
| GET | `/api/vault/excluded-folders` | `/api/vaults/{vault_id}/excluded-folders` |
| GET | `/api/vault/exclusions/item` | `/api/vaults/{vault_id}/exclusions/item` |
| GET | `/api/vault/exclusions/inclusion-preview` | `/api/vaults/{vault_id}/exclusions/inclusion-preview` |
| POST | `/api/vault/exclusions/exclude` | `/api/vaults/{vault_id}/exclusions/exclude` |
| POST | `/api/vault/exclusions/include` | `/api/vaults/{vault_id}/exclusions/include` |
| GET | `/api/vault/move` | `/api/vaults/{vault_id}/move` |
| POST | `/api/vault/move` | `/api/vaults/{vault_id}/move` |
| GET | `/api/vault/lifecycle` | `/api/vaults/{vault_id}/lifecycle` |
| PUT | `/api/vault/lifecycle` | `/api/vaults/{vault_id}/lifecycle` |

13 operations on 11 paths (the issue estimated 10). None collides with an
existing `/api/vaults/{vault_id}/…` path. Other query parameters (`directory`,
`path`, `is_directory`) and request bodies stay as they are, except
`VaultMoveRequest`.

What the move keeps and what it drops:

- The moved routes accept the same Vaults as today: operational ones
  (`active`, `reconnect_required`); any other id answers 404
  `api.protected_vault_not_found`. One dependency in `app/routes/deps.py` reads
  the path id with that rule; it never reads or writes the Session.
- `vault_id` below 1 stays a 422.
- Dropped: the Session fallback on these 13 operations, with its 403
  `api.no_vault` and 409 `api.session_changed`; the 409
  `api.vault_move.vault_changed` and its message, since a body cannot disagree
  with a path it no longer repeats. `VaultMoveRequest` stays `extra="forbid"`:
  a client that still sends `vault_id` gets 422.

### The other 24 operations

The rule is mechanical: the path after `/api/` goes after
`/api/vaults/{vault_id}/`, and `?vault_id=` goes away.

| Method | Old address | New address |
|---|---|---|
| GET | `/api/files` | `/api/vaults/{vault_id}/files` |
| GET | `/api/files/detail` | `/api/vaults/{vault_id}/files/detail` |
| GET | `/api/files/versions` | `/api/vaults/{vault_id}/files/versions` |
| GET | `/api/files/jobs` | `/api/vaults/{vault_id}/files/jobs` |
| POST | `/api/files/cloud-only-conflict/replace` | `/api/vaults/{vault_id}/files/cloud-only-conflict/replace` |
| GET | `/api/stats` | `/api/vaults/{vault_id}/stats` |
| GET | `/api/jobs` | `/api/vaults/{vault_id}/jobs` |
| GET | `/api/jobs/{job_id}` | `/api/vaults/{vault_id}/jobs/{job_id}` |
| POST | `/api/jobs/cancel` | `/api/vaults/{vault_id}/jobs/cancel` |
| GET | `/api/job-groups/{group_id}/jobs` | `/api/vaults/{vault_id}/job-groups/{group_id}/jobs` |
| POST | `/api/scan` | `/api/vaults/{vault_id}/scan` |
| GET | `/api/catalog/revision` | `/api/vaults/{vault_id}/catalog/revision` |
| GET | `/api/catalog/events` | `/api/vaults/{vault_id}/catalog/events` |
| POST | `/api/upload` | `/api/vaults/{vault_id}/upload` |
| GET | `/api/upload/admissions/{group_id}` | `/api/vaults/{vault_id}/upload/admissions/{group_id}` |
| POST | `/api/upload/reconcile` | `/api/vaults/{vault_id}/upload/reconcile` |
| POST | `/api/upload/cancel-pending` | `/api/vaults/{vault_id}/upload/cancel-pending` |
| POST | `/api/upload/retry-failed?vault_id=` | `/api/vaults/{vault_id}/upload/retry-failed` |
| POST | `/api/recover` | `/api/vaults/{vault_id}/recover` |
| POST | `/api/recover/estimate` | `/api/vaults/{vault_id}/recover/estimate` |
| POST | `/api/recover/approve` | `/api/vaults/{vault_id}/recover/approve` |
| POST | `/api/free-space` | `/api/vaults/{vault_id}/free-space` |
| POST | `/api/cloud-deletion/preview` | `/api/vaults/{vault_id}/cloud-deletion/preview` |
| POST | `/api/cloud-purge` | `/api/vaults/{vault_id}/cloud-purge` |

None collides with an existing path. The pull request that moves a route
checks the table against the code and corrects it if a route was missed.

- `POST /api/upload/retry-failed` without an id already means the whole
  installation, never the Session's Vault. It stays at that address for that
  meaning and loses its `vault_id` query parameter; one Vault's retry is the
  new path. Unlike the others, the per-Vault retry accepts any existing Vault
  as today, not only operational ones.
- Installation-wide reads stay where they are: `/api/files/search`,
  `/api/upload-activity`, `/api/general…`, `/api/problems`,
  `/api/cloud-deletion`.
- When the last route has moved, `current_vault` and the `?vault_id=`
  parameter are deleted. The Session's Vault stops being an address for any
  operation.
- **The Session holds no Vault** (the Owner's decision of 2026-11-24, step 5).
  The interface opens on the Vault of its route, else the one remembered in
  the browser, else the first listed. `POST /api/vaults/select` and the
  `vault` field of `GET /api/me` are removed; `/api/me` no longer writes the
  first operational Vault into the Session, and the catalog event stream
  neither reads it nor sends `current_vault_id` or `vault_switched` in its
  error frames. `stopped_vault` stays, reported only while no Vault is
  operational, in the order once used for a Session without a Vault:
  Stopping first, then the latest root release, then the name. The
  `sessions.vault_id` column stays in the schema, unwritten and unread.

`StorageEstimateRequest.vault_id` is not an address: it selects what to
estimate, as an alternative to `size_bytes`. It stays.

## Migration

Several pull requests under #805, in this order. Each one lists every changed
address and every changed schema name in its description, for the release
notes. Sizes exclude the generated `openapi.json` and TypeScript.

1. **Addressing, Vault settings.** `app/routes/policy.py` (9 routes), `app/routes/storage_plan.py`
   (4 routes, `VaultMoveRequest`), `app/routes/deps.py` (the path dependency),
   `app/locales/en.json` and `it.json` (one message removed),
   `frontend/src/api/vault.ts` (13 calls from `scoped` to `owned`), 10 Python
   test modules (70 references) and 11 frontend test files (46 references).
   About 300 lines, most of them test addresses.
2. **Addressing, browsing and Jobs.** The 24 other operations, in two pull
   requests: `app/routes/browse.py` (files, stats) with its frontend calls;
   then `app/routes/jobs.py` (Jobs, scan, catalog revision and event stream,
   Uploads, recovery, cloud deletion) with `frontend/src/api/catalogEvents.ts`.
   The second deletes `current_vault`. Each proves that the SQL emitted per
   request does not grow and that the event stream reconnects as before.
3. **The pipeline.** The pass-through declaration and the test-time check
   (`app/api_models.py`, `tests/fixtures.py`, `tests/test_openapi_contract.py`),
   `scripts/export_openapi.py` and the CI step (`.github/workflows/migrations.yml`,
   `docs/ci.md`), removal of the 9 orphans and of the dead
   `LifecycleProfileSelection`, with the six frontend imports renamed to the
   Pydantic models. This pull request proves on one route that the response
   bytes and the per-request cost do not change. About 250 lines, plus the
   orphans' entries deleted from the JSON file.
4. **Schemas, one router per pull request.** Each moves its response schemas
   from the JSON file into Pydantic models in the router module, replaces its
   bare `JsonObjectResponse` declarations, and fixes the mismatches the check
   finds by correcting the model, never the route's payload:
   - `sign_in.py`: 7 operations (`login`, `logout`, `reauth` untyped today). About 150 lines.
   - `vaults.py`: 29 operations with the inventory router. About 500 lines.
   - `browse.py`: 16 operations (two are raw downloads), including `GET /api/problems` and four untyped ones. About 400 lines.
   - `jobs.py`: 22 operations (one is the event stream), six untyped. About 500 lines.
   - `policy.py` and `storage_plan.py`: 14 operations. About 300 lines.
   - `ops.py` (`/health`, `/ready`) and the end: `app/openapi_schemas.json`,
     `response_model()`, `documented_schemas()` and the merge in `app/main.py`
     are deleted. About 100 lines.

   Until the last one, the merge kept serving the schemas not yet moved. After
   it, `ApiErrorResponse` is a Pydantic model in `app/routes/deps.py`, the
   module that declares the documented refusals, and FastAPI's own
   `app.openapi()` writes the document.
5. **No Vault in the Session.** `POST /api/vaults/select`, the `vault` field
   of `GET /api/me`, the Session write in `/api/me` and the Session read of
   the catalog event stream are removed, with `selectVault` in the frontend.
   `/api/me` reads the Vaults with one statement instead of one to three plus
   a Session write.

Pydantic writes JSON Schema differently from the hand-written file (titles,
`anyOf` for nullable fields). Each schema pull request shows the diff of
`openapi.generated.ts`; the frontend typecheck is the proof that the types
callers see did not change, apart from the mismatches it lists.

## Alternatives rejected

- **`openapi.json` as the source, Python generated from it.** The request
  models already live in Python and validate real input; a second generator
  would stand between the route and its contract.
- **Keep the hand-written file and only test it against responses.** The
  double declaration and the orphans stay; every new field is still typed
  twice.
- **Validate responses in production** (FastAPI's ordinary `response_model`).
  It filters undeclared fields and validates every payload, the file listings
  included: a behaviour change and a cost on every request for a
  single-Owner application whose only client ships in the same image.
- **Keep `/api/vault/…` as aliases for a release.** The only client is the
  bundled frontend, updated in the same pull request; an alias would keep the
  Session fallback alive on exactly the routes this decision takes it from.
- **Keep `vault_id` in the body of `VaultMoveRequest` as a double check.** It
  guarded against the Session's Vault changing between preview and
  confirmation; with the id in the path there is nothing left to compare.
- **Sort the keys of the committed `openapi.json`.** It would make the script
  right by rewriting 18,348 lines once; dropping `sort_keys` reaches the same
  agreement with no diff, and the route order is already pinned by a test.

## Consequences

- This narrows ADR-0022 for the 37 moved operations: its "unscoped legacy API
  consumers still use Session selection" no longer applies to them. A script
  or bookmark calling an old address gets 404; the release
  notes list the table above.
- Every response model, and every model it nests, derives from
  `ResponseModel` (`app/api_models.py`), which sets Pydantic's `defer_build`:
  production never validates with these models, so they are built when the
  document is generated or a test checks an answer, not at import. Defining
  the classes still costs at import; only building their validators moves.
- A field added to a route without its model fails the tests when the model is
  closed; for an open model the check covers only the declared fields. Whether
  a model is open or closed is copied from the hand-written schema it
  replaces, not tightened during the move.
- The generated component names the frontend imports change where an orphan
  is replaced by the Pydantic model; no payload changes.
- The contract is only as checked as the tests that call a route: an operation
  no test calls is documented but unverified. The check reports which
  operations it never saw, so the gap is visible.
- "One address for a Vault" is true for the Vault's own settings after step
  1, and for browsing, Jobs and Uploads after step 2.
- A request without a Vault no longer falls back to the Session's Vault or to
  the first operational one: the 403 `api.no_vault` and 409
  `api.session_changed` answers of these routes go away with the fallback.
- A browser with no Vault in its route or memory opens the first Vault
  listed, no longer the one another browser of the same Session last chose.
