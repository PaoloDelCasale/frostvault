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

Implementation and validation in progress. This document will be updated before
handoff with files, targeted test commands/results, exact CI run evidence and
remaining limitations. The PR must remain draft and unmerged for parent review.
