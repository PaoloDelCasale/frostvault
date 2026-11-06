# Issue #463 — local readiness probe handoff

## Scope and decisions

- Issue: https://github.com/PaoloDelCasale/frostvault/issues/463
- Adjacent integration: https://github.com/PaoloDelCasale/frostvault/issues/462
  integrated first through merged PR #467 at
  `5a74e8e8ca897d128db28817fc467343fed249d0`. This change does not implement or
  modify its explicit proxy configuration.
- Read the updated issues and comments (none at implementation start), AGENTS.md,
  CONTEXT.md, issue-tracker instructions, ADR-0004 and ADR-0005.
- Native relationships checked with GraphQL: neither issue has a parent or native
  blocked-by/blocking issues; #463 has no subissues or project items.
- Owner selected **authorized Host in the probe**. No middleware exceptions for
  loopback, `/health`, or `/ready`. No weakening of authentication/proxy trust.
- Plan: image-level exec-form healthcheck, inherited by both Compose templates;
  Python standard-library request to fixed `127.0.0.1:8080/ready`, with an exact
  safe Host selected from `ALLOWED_HOSTS`. Fail on readiness 503, redirects,
  invalid Host configuration, timeout, or connection failure; no liveness fallback.

## Safety boundaries

Repository changes, dedicated draft PR and CI only. No live Docker/Compose,
restarts/deploys, system packages, real databases, S3, credentials, `/boot`, or
backup shares. Local Python 3.12 `.venv` with requirements installed; only
targeted database-independent modules run locally. Real PostgreSQL, S3-compatible,
frontend and image coverage must come from PR CI, not silent local skips.

## Acceptance evidence

Dedicated draft PR: https://github.com/PaoloDelCasale/frostvault/pull/465
Branch: `fix/463-local-readiness-probe`.
Worktree: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-f7b931d8`.
Session: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-f7b931d8/sessioni/2026-11-06-463-readiness-probe`.

Implemented files:
- `app/readiness_probe.py`: standard-library probe, exact safe Host selection,
  fixed local socket, no proxies/redirects/body logging, only HTTP 200 succeeds.
- `Dockerfile`: exec-form healthcheck (30s interval, 5s Docker timeout, 60s
  startup grace, 3 retries; 3s socket timeout in the probe).
- `tests/test_readiness_probe.py`: 13 focused tests including real middleware
  and `/ready`, mocked dependency seams (no DB), loopback HTTP transport, empty
  and multiple Hosts, injection/unsupported configuration, redirects, failures,
  unchanged external and loopback Host rejection and standard-library-only CLI.
- `.github/workflows/migrations.yml`: production-image job also verifies its
  healthcheck metadata and runs packaged Host/HTTP/failure tests read-only as
  UID/GID 99:100; no live service or new public port.
- `.env.example`, `README.md`, `docs/traefik.md`, `docs/ci.md`: Host syntax,
  inherited healthcheck, verification, actual readiness/failure semantics.
- Neither Compose template nor Host/proxy/auth middleware was modified.

Local validation:
- Created Python 3.12 `.venv` and installed `requirements.txt` successfully.
- After integrating #462:
  `.venv/bin/python -m unittest tests.test_readiness_probe tests.test_ci_contracts tests.test_public_release_hygiene tests.test_forwarded_proxy -v`
  **58 tests passed**. Log: `targeted-tests.log` in this session directory.
- Parsed both Compose templates and the changed CI YAML with PyYAML.
- `git diff --check` passed.
- Existing Starlette/httpx and Authlib deprecation warnings only.
- No local database coverage claimed or skipped: real PostgreSQL, S3-compatible,
  frontend/browser/image verification remains exclusively in PR CI.

## CI evidence and integration

- Initial implementation CI run `42139141139` passed image/probe, PostgreSQL,
  S3-compatible, frontend/Playwright checks except one unit shard: a fake test
  credential's domain violated the existing public-release hygiene rule. The
  exact targeted hygiene test reproduced it locally (red); only the fixture was
  changed to the permitted `example.com` domain. No gate was bypassed/weakened.
- Fixed implementation commit `b6ee22725de0cc76fd95df0ba32bfc1d72863a30`:
  **28/28 checks completed SUCCESS**, verified through PR check runs.
  - CI: https://github.com/PaoloDelCasale/frostvault/actions/runs/42139639397
  - Security: https://github.com/PaoloDelCasale/frostvault/actions/runs/42139639392
- Merged latest `origin/main` normally (no rebase/force push) after the coordinator
  merged #467. Resolved the one documentation conflict by retaining all of
  #462's trust-layer explanations, explicit required allowlists and routing/auth
  preservation guidance, then appending #463's separate local-readiness section.
- Verified **no diff from integrated main** in either Compose template,
  `app/main.py`, `app/proxy.py` or ADR-0005. Both required proxy lists and the new
  proxy tests remain intact; Host middleware has no exception or change.
- Integrated implementation/merge commit
  `c734a3f5635b5536a18a0092df054d64e0d4a1e0`: **28/28 checks COMPLETED/SUCCESS**.
  The full check-run metadata (HEAD, base, draft/open state, statuses, URLs and
  timestamps) is committed in this session's `ci-integrated.json`.
  - CI: https://github.com/PaoloDelCasale/frostvault/actions/runs/42141028048
  - Security: https://github.com/PaoloDelCasale/frostvault/actions/runs/42141028346
  - Includes all twelve PostgreSQL unit shards + aggregate, migrations/concurrency,
    S3-compatible integrity, frontend generation/typecheck/lint/Vitest Node 22/24,
    production frontend build, Playwright mobile/desktop, production-image backup
    and packaged probe checks, dependency audits, CodeQL, Gitleaks and Trivy.
- Refetched main before recording evidence: still
  `5a74e8e8ca897d128db28817fc467343fed249d0`, already an ancestor. Re-read updated
  #463 comments and confirmed the preserved #462 integration requirements.
- The final documentation/evidence commit is checked again by full PR CI before
  handoff. Its exact HEAD and green check status are reported in the PR handoff
  comment and final response, avoiding a self-referential commit hash here.

## Acceptance mapping

- Configured/empty/multiple allowlists: first safe exact Host (case preserved)
  reaches real `/ready` over a fixed loopback socket. Public DNS and proxy env
  cannot redirect the probe.
- Security: CR/LF/NUL, shell fragments, userinfo, URLs, ports and wildcards cannot
  become headers or commands; an all-unsafe non-empty list fails before any I/O.
  No values, response bodies or exception details are logged.
- Host perimeter unchanged: unauthorized Host is 400 from both loopback and an
  outside peer, including `/health`, `/ready`, `/api/me`, and forwarded-Host spoof.
- Dependency/worker/config/source-layout 503 fails even while `/health` is 200;
  all redirects and other non-200 statuses fail without fallback or follow.
  Timeouts, refused connections and malformed HTTP also fail.
- Packaging: exec-form image healthcheck is inherited unchanged by both supplied
  Compose services; no labels/routes/networks/volumes/restart policy/auth edits.
  Probe imports/runs with the standard library only; image tests additionally run
  it under numeric 99:100 with read-only rootfs and all capabilities dropped.
- No restart/reload is required or authorized as part of this PR. A future image
  release/deployment requires the Owner's separate approval.

## Limitations / operator follow-up

- No production deploy/restart, local image/frontend build, database or S3 work.
- CI uses disposable PostgreSQL 16 and S3-compatible moto (historical job name
  MinIO), not live AWS/Glacier proof. Dependency seams in local tests are mocked;
  real PostgreSQL coverage is provided by CI, never silently skipped locally.
- The probe is local readiness, not proof of public Traefik/TLS/OIDC availability.
- The internal port is 8080, matching the image command; `APP_PORT` is host-only.
- Multiple allowlist entries work for the probe; the reference Traefik router
  still expects one hostname. Wildcards/URLs/ports are not probe Host entries.
- Startup grace is 60s; operators can explicitly override it for known long
  migrations. Health status does not cause `unless-stopped` to restart a process.
- Parent retains review, any deployment authorization and final merge. This PR
  must remain **draft and unmerged**.
