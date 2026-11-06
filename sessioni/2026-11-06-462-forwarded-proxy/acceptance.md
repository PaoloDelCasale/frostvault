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

Initial targeted run: **35 tests PASS**; rerun after final edge cases to follow.
The DB-free test exercises `uvicorn.Config.load()` using the actual environment
variable, actual application Host middleware, OIDC route/authorization builder,
client resolver and Emergency Password denial handler. Only persistence and
OIDC network I/O use test seams. Both unsafe directions of allowlist mismatch
are explicit counterexamples, not accepted deployment configurations.

`compose-validation.json` records actual Docker Compose **config-only** tests:
coherent explicit values pass; missing or empty values for either allowlist
fail (five cases total). YAML parses. Comparing parsed Compose against the base
shows labels, networks, mounts, identity, security controls and every non-proxy
field unchanged. No Docker daemon action/build/start/restart was performed.
`py_compile` passed for new/changed proxy HTTP tests; `git diff --check` passed.
Only existing Starlette/Authlib deprecation warnings were observed.

## CI and remaining handoff

Dedicated draft PR: https://github.com/PaoloDelCasale/frostvault/pull/467
Fixes #462. PR opened early and ordering notes posted to #462/#463. Full CI
verification is in progress; final exact-HEAD evidence will be recorded here.

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
