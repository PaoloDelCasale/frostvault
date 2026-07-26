# ADR 0008: Tailwind 4 + shadcn/ui design system

## Status

Accepted

## Context

The legacy UI used 286 lines of hand-written CSS with one breakpoint and no
shared component primitives. Epic #56 required comfortable mobile use (drawer,
bottom sheet, 44×44px targets) without a rebrand: the FrostVault palette and
state badges had to survive the migration.

## Decision

- Adopt **Tailwind CSS 4** with a utility-first workflow and map the existing
  palette into `@theme` tokens (`--ink`, `--muted`, `--line`, `--surface`,
  `--canvas`, `--green`, soft state colours, and the card/panel/auth/badge
  radii).
- Vendor **shadcn/ui** components into the repository (no CDN). Compose
  FrostVault surfaces (`Card`, `Panel`, `AuthCard`, `Badge`, sheets, dialogs)
  on top of those primitives.
- Use **system fonts only**. Do not load webfonts; Traefik CSP is
  `default-src 'self'; script-src 'self'`.
- Keep visual identity continuous with the pre-SPA UI: this is a migration of
  tokens and components, not a new brand.

## Consequences

- Styling lives in `frontend/src/index.css` `@theme` and component classNames;
  contributors no longer edit a hand-written global stylesheet for the UI.
- Dark mode is intentionally out of scope for epic #56; the token layout makes
  it a later change (#83).
- Design-system and shell work (#62) owns the shared layout (skip link, drawer,
  sticky search/breadcrumbs); feature pages compose those primitives.
