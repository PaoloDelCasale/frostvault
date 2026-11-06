# Issue #462 — implementation and acceptance handoff

## Scope and decisions

- Dedicated branch `feat/trusted-proxy-allowlists`; prepared T3 worktree
  `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-05c82f87`.
- Two explicit, narrow and coherent allowlists: app `TRUSTED_PROXIES` and
  Uvicorn `FORWARDED_ALLOW_IPS`. No automatic derivation or wildcard workaround.
- Preserve networks, mounts, labels, auth, Host middleware and image command.
- #463 follows this PR: readiness healthcheck belongs to its separate worktree;
  integrate #462 first. No healthcheck implementation in this branch.
- Owner authorized repository edits, commits, push, draft PR and autonomous CI
  corrections, not merge or any production action.

## Specification inspection

Read `AGENTS.md`, `CONTEXT.md`, `docs/agents/issue-tracker.md`, issue #462 and
#463 full bodies/comments, #412 and parent #410, and ADR-0004/0005/0017.
Native GraphQL relationships: #462 and #463 have no parent or dependencies;
#412 has parent #410, blocked by #408/#409/#411, blocks #418 (read these too).
`gh issue view` reports no Project items on #462/#412. Project field GraphQL
access is unavailable with the existing token's scopes; no credential change
attempted. The explicit issue decision and owner-provided ordering govern work.

## Runtime finding / unsafe disagreement

Inspected installed pinned Uvicorn 0.54.0 `config.py` and
`middleware/proxy_headers.py`, not just documentation. Proxy headers are enabled
by default. Uvicorn reads `FORWARDED_ALLOW_IPS` independently (unset defaults to
`127.0.0.1,::1`; empty trusts nobody), checks the socket peer, rewrites scheme
AND ASGI client, and leaves original headers present. FrostVault then resolves
IP using that ASGI client and `TRUSTED_PROXIES`; it does not process forwarded
scheme itself or recover the original peer. Thus an app list cannot veto trust
already granted by Uvicorn. Broader lists at either layer are unsafe. Matching
narrow proxy-only lists plus correct proxy header sanitization are required.
No runtime code is broadened to hide this disagreement.

## Acceptance coverage

- DB-free targeted integration through `uvicorn.Config.load()` and actual app
  routes: trusted HTTPS OIDC callback; untrusted scheme spoof refusal and
  Emergency Password loopback/LAN spoof denial; multi-hop chains; independent
  list behavior and unsafe disagreement; unchanged Host gate.
- PostgreSQL-backed route regressions in CI: actual OIDC state persistence and
  successful allowed-LAN Emergency Password / refused outside request through
  real Uvicorn proxy middleware.
- Targeted deployment contract checks; YAML parse and Compose config validation
  without any `up`, Docker build or production service action.
- Full Python/PostgreSQL/S3-compatible/frontend/browser/image/security checks
  only in draft PR CI, on final HEAD. No local DB tools or production DB use.

## Local verification

Python 3.12 `.venv` created and `requirements.txt` installed (Uvicorn 0.54.0).
Only targeted tests executed, without a local database, external provider or S3:

```bash
.venv/bin/python -m unittest \
  tests.test_forwarded_proxy tests.test_proxy tests.test_breakglass tests.test_config \
  tests.test_container_hardening.TraefikReferenceDeploymentTests \
  tests.test_container_hardening.PermissionAndTraefikDocumentationTests -v
```

Final targeted run: **36 tests PASS** (0.768 seconds), including missing,
empty and malformed forwarded client data. No local database tests were run.
The DB-free test exercises `uvicorn.Config.load()` using the actual environment
variable, actual application Host middleware, OIDC route/authorization builder,
client resolver and Emergency Password denial handler. Only persistence and
OIDC network I/O use test seams. Both unsafe directions of allowlist mismatch
are explicit counterexamples, not accepted deployment configurations.

`compose-validation.json` records actual Docker Compose **config-only** tests:
coherent explicit values pass; missing or empty values for either allowlist
fail (five cases total). YAML parses. Comparing parsed Compose against the base
shows labels, networks, mounts, identity, security controls and every non-proxy
field unchanged. No Docker daemon mutation/build/start/restart was performed.
The existing hardening-test module performs read-only Docker availability
checks (`docker info` / `docker compose version`) at import.
`py_compile` passed for new/changed proxy HTTP tests; `git diff --check` passed.
Only existing Starlette/Authlib deprecation warnings were observed.

## CI evidence and handoff

Dedicated draft PR: https://github.com/PaoloDelCasale/frostvault/pull/467
Fixes #462. PR opened early and ordering notes posted to #462/#463.

Implementation commit `2d37b2b5b86614bc3c17927152b08435d8bebeb9`: **28/28
check runs COMPLETED/SUCCESS**, no failures or skipped check runs. No CI gate
was weakened, bypassed, or replaced by local coverage.

- CI: https://github.com/PaoloDelCasale/frostvault/actions/runs/42139333179
- Security: https://github.com/PaoloDelCasale/frostvault/actions/runs/42139333226
- `ci-implementation.json`: exact implementation SHA and all check conclusions.
- `ci-proxy-evidence.txt`: selected actual CI route test output.
- `gh run view <id> --log-failed` inspected for both runs; no failed logs.

Coverage includes all twelve PostgreSQL Python shards, migration/concurrency,
S3-compatible integrity (historical MinIO name; ephemeral moto, not live AWS),
frontend typecheck/lint/Vitest Node 22/24/build, Playwright desktop/mobile,
production-image PostgreSQL backup, CodeQL, gitleaks, pip/npm audit and image
vulnerability scan. The new PostgreSQL route tests passed in CI: HTTPS OIDC
begin/callback agree on the URI and persist state/session IP; allowed LAN
Emergency Password succeeds; spoofed outside/near-miss clients and password
reauthentication are refused, without creating sessions/backoff rows.

The follow-up commit only records this handoff/evidence. Its own final CI must
also be green before returning to the coordinator. The final PR HEAD/check
snapshot is reported in the PR and task response rather than pretending a
versioned document can embed its own commit hash. To independently recheck:

```bash
gh pr view 467 --json isDraft,headRefOid,mergeStateStatus
gh pr checks 467
gh api 'repos/PaoloDelCasale/frostvault/commits/<HEAD>/check-runs?per_page=100'
```

Self-review: acceptance traced against #462/#463 and ADR-0005/0017, no change to
app runtime, auth, Host gate, Dockerfile command or healthchecks. Existing ADR
wording inaccurately attributed scheme handling to app trust; corrected inline
with the actual two-layer behavior and Owner-approved explicit-list decision.
No external reviewer/subagent was started. Leave draft and **do not merge**.

## File inventory / inspection

Changed deployment/docs: `.env.example`, `compose.traefik.yaml`, `README.md`,
`docs/traefik.md`, `docs/adr/0005-host-csrf-reauth-hardening.md`.
Changed tests: `tests/test_oidc_http.py`, `tests/test_breakglass_http.py`,
`tests/test_container_hardening.py`; added `tests/proxy_stack.py` and
`tests/test_forwarded_proxy.py`. Evidence is confined to this session folder.

Also inspected `app/proxy.py`, `app/main.py` (client, Host/auth/OIDC routes),
`app/config.py`, `app/breakglass.py`, `app/oidc.py`, `app/sessions.py`, audit
storage, `Dockerfile`, `docker/entrypoint.sh`, `requirements.txt`, existing proxy,
config, OIDC, Emergency Password and deployment tests, `docs/ci.md`, workflow
configuration and installed Uvicorn config/middleware. No API/schema change or
frontend artifact regeneration was needed.

### Risks / integration constraints

- Compose now intentionally refuses missing/empty proxy allowlists. Operators
  must inspect their topology and set **both**, including when adopting #463.
- Equivalence is documented, not runtime-enforced; broader lists at either layer
  can defeat the perimeter. Do not treat app trust as a veto on Uvicorn.
- Stable proxy-only source addresses and correct upstream header sanitization
  are required. Shared host/gateway source IPs cannot distinguish local processes.
- No real Traefik/OIDC/production acceptance was attempted. In-process and CI
  fixtures prove application behavior, not a site's actual network topology.
- No app runtime/Host changes, healthcheck, migration, production credentials,
  live Compose changes, deployment, backup copies or merge. Integrate this draft
  before #463 only after owner/parent review.
