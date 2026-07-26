# ADR 0007: React SPA replaces Jinja templates

## Status

Accepted

## Context

FrostVault's UI was six standalone Jinja documents plus hand-written static JS
and a single-breakpoint CSS file. That stack could not meet the product
requirement of full functional parity on a 375px phone (drawer navigation,
44×44px targets, accessible confirmations, cards instead of a five-column
table). Epic #56 migrated behind a temporary SPA feature flag and then removed
Jinja entirely at cut-over (#71).

## Decision

- Ship a **React 19 + TypeScript + Vite 8** single-page application as the only
  frontend. FastAPI serves `frontend/dist` for every HTML route and falls back
  to `index.html` for client routes; JSON under `/api/*` and `/auth/*` is
  unchanged.
- Build the SPA at image-build time (Node stage in the Dockerfile). Production
  runs only Python; Node is not a runtime dependency.
- Remove the temporary SPA feature flag, every Jinja HTML template, and the
  legacy static JS/CSS that backed those pages. No dual-frontend fallback
  remains after cut-over.
- Keep i18n catalogs in `app/locales/{en,it}.json` served by
  `/api/i18n/catalog`, consistent with ADR-0006; the SPA renders through an
  i18n provider instead of Jinja `t()` / embedded `#i18n-catalog`.
- Capabilities (`can_operate`, `delete_enabled`, …) come from `/api/me`, not
  from server-rendered `data-*` attributes.

## Rejected alternatives

- **Hand-written mobile-first CSS on the Jinja pages** — would not fix
  `innerHTML` string contracts, inaccessible `window.prompt`/`confirm`, or the
  lack of a component model for sheets and drawers.
- **Tailwind on top of Jinja** — still leaves vanilla JS and duplicated page
  documents; TypeScript contracts would remain implicit inside HTML strings.
- **An SSR meta-framework with a Node runtime in production** — conflicts with
  the single-process Python deploy, Traefik CSP (`script-src 'self'`, no CDN),
  and the existing session/CSRF model.

## Consequences

- Contributors must run `cd frontend && npm ci && npm run build` (or use the
  Vite dev server) before HTML routes work against uvicorn.
- Anti-XSS coverage moves from the legacy static-JS safety suite to an ESLint
  ban on `dangerouslySetInnerHTML` plus React rendering tests for hostile file
  names.
- Legacy Node `vm`-sandbox UI tests are replaced by Vitest suites; Playwright
  covers mobile and desktop flows.
- PWA installability and push (#72) become possible on top of the SPA shell.
