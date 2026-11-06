# Issue #463 — local readiness probe handoff

## Scope and decisions

- Issue: https://github.com/PaoloDelCasale/frostvault/issues/463
- Adjacent integration: https://github.com/PaoloDelCasale/frostvault/issues/462
  must integrate first; this change does not implement proxy configuration.
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
- `.venv/bin/python -m unittest tests.test_readiness_probe tests.test_ci_contracts -v`
  **45 tests passed**. Log: `targeted-tests.log` in this session directory.
- `git diff --check` passed.
- Existing Starlette/httpx and Authlib deprecation warnings only.
- No local database coverage claimed or skipped: real PostgreSQL, S3-compatible,
  frontend/browser/image verification remains exclusively in PR CI.

CI validation and #462 integration status will be recorded before handoff.
The PR must remain draft and unmerged for parent review.
