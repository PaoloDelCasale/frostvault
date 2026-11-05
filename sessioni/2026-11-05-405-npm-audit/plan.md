# Issue #405 — remove build-toolchain npm advisories

Authorized by Paolo: implement a targeted fix, open a PR, verify audits and full PR CI, review and merge only if safe; close #405 only when both advisories are eliminated. No production changes/deploy.

Base: main `3e0b4cd8c0d0d89efaaff53f6783454188501433`.
Native parent/dependencies/blocking: none. Issue text predates PWA removal and the narrowed minimatch advisory (currently >=7 <11.0.1).

## Investigation

- Full lockfile audit still reports GHSA-gcqx-w48m-w2h4 (`deepmerge` 4.3.1 via shadcn) and GHSA-g3hv-75wg-m249 (two minimatch 10.2.6 copies). Runtime-only audit clean.
- `deepmerge` latest is still 4.3.1. `minimatch` patched latest is 11.0.4; project supports Node ^20.19 or >=22.12, compatible with its Node 20 or >=22 requirement. Latest typescript-eslint still requires minimatch ^10.2.2.
- shadcn is used only through the CSS import and vendored UI conventions, not as an application JS import. Preserve exact CSS behavior/attribution rather than simply deleting the import. Inspect the installed package before choosing the minimal solution.
- Avoid broad overrides of safe minimatch 3/5 and avoid npm audit fix --force.

## Planned proof

1. Open a draft PR early; inspect shadcn CSS/license and the actual minimatch consumer API.
2. If safe, vendor the needed CSS with provenance/license and remove the CLI dependency graph; constrain a minimatch 10-only override to the patched release.
3. Regression contracts for vulnerable package absence, scoped override, retained CSS, and exception retirement; focused minimatch consumer compatibility tests.
4. Local targeted Python/Vitest/node tests, audit full/runtime plus strict existing gate. Full frontend/backend/Postgres/S3/browser/build/image checks only in PR CI.
5. Review final diff and exact HEAD checks before squash merge/closure. No exception renewal, skipped security checks, production services/data, backups, restart or deploy.
