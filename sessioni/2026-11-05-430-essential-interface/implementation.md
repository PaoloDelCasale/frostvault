# #430 — Essential Owner interface

## Boundaries

- Base: `origin/main` **bf1e3fd6082c4ff8e662ab6f2815cb6227f23bb7**, after #428/#429. Separate worktree and `issue/430-essential-interface` branch.
- Draft PR: https://github.com/PaoloDelCasale/frostvault/pull/455 . No merge, issue closure or epic update; coordinator owns those actions.
- Read #430, native parent #410 and closed native blockers #423–429; #360, #365 D1–D17 and #366 resolution/comments, CONTEXT and ADRs 0001–0021. Prototype read via `git show`, visual B reference only. No prototype code/data enters the application.
- GitHub token lacks `read:project`; project field lookup unavailable, no project writes attempted.
- No delegates, local services, PostgreSQL, MinIO, Playwright, production builds or images. No deployment, production credentials or data accessed. Artifacts stay in this session directory; no backup copies created.

## Implementation

Browser-first sidebar, current protected-Vault root home persisted per Owner/browser, mobile navigation dialog, four filters and global name/path search. Request-bound `vault_id` scopes every mutation, preview, Details lookup and live catalog stream. Explicit invalid/stopped scopes fail closed without modifying Session selection. Global search is SQL-counted/paged, preserves duplicate-path Vault identities and applies source-observation state before filtering. The SSE retains authentication, session expiry/revocation and Owner checks without following another tab's selected Vault (ADR 0022).

New General and open-Problems surfaces replace the administrative/access shell. General reuses guarded Recovery Kit/custody, encrypted metadata backups, Source Volume inventory and reconnect components. Optional SMTP stays deployment-owned; new optional `APP_PUBLIC_URL` produces email deep links. These open the parent folder, exact file outside the first page if necessary, highlighted row and Details/remedy. Financial figures are AWS list-price **storage run-rate estimates**, not actual billing, budgets or monthly usage projections. Delta uses a retained previous-calendar-month weekly-summary baseline, otherwise explicitly unavailable.

Selection exposes only Recover, Free space now and one-file Details. Cloud class/version/integrity stay in Details (or the explicitly advanced Storage Plan UI). Recovery defaults to Economical/Bulk, offers only Bulk/Standard when cold restoration is needed, previews file/folder costs/times, pins the previewed exact file snapshot, explains return path and local retention, and retains high-impact approval/cancellation safeguards. A small active-work surface handles only Owner-requested recovery/release and Vault Move, not routine per-file events. Deliberate cloud deletion remains inside Details with #428 reauth, typed confirmation, preview, cancellable seven-day delay and server-side safety checks. Per-Vault gear reuses automatic protection, fixed release periods, collapsed Storage Plan/Move now and Stop Protection.

Removed superseded administrative/access/demo/old browser surfaces, Mixed badge, old no-Vault standalone page, obsolete tests/translations and screenshot commands together. Surviving APIs moved to `/api/general/*` and explicit reconnect. OpenAPI/TypeScript regenerated. README, CI documentation, example deployment URL and ADR 0022 updated.

## D1–D17 acceptance map

| Decision | Implemented evidence |
|---|---|
| D1 | `App`, `AppShell`, sidebar filters, browser search, General; mobile drawer. |
| D2 | Top open-problem count, all-Vault Problems page, per-Vault indicator and browser banner. |
| D3 | SMTP/frontend deep-link helpers; exact scoped lookup, parent directory, highlight, Details and remedy. |
| D4 | Owner-specific last-used Vault identity, reopened from root, no persisted listings. |
| D5 | Folder counts for the five states; no Mixed badge/token/type. |
| D6 | Name, Protection, Size, Last modification, Ready in; internal cloud fields only Details. |
| D7 | SQL-paged global protected-Vault name/path search, path and Vault on results; same-path identities distinct. |
| D8 | No previews, EXIF/date-taken search or photo-manager behavior. |
| D9 | Selection only Recover, Free space now, single-file Details; deliberate deletion only inside Details. |
| D10 | Bulk default / Standard only when needed; file/folder estimates, destination, local retention, irreversible-restore warning and exact snapshot pinning. |
| D11 | Gear and collapsed advanced Storage Plan/Move; General kit/fingerprint/custody/verified backup, Source Volumes, reconnect/create, per-Vault estimate; deployment-owned optional email. |
| D12 | Existing fixed problem/recovery/weekly notification semantics reused; no routine upload/verification event feed. |
| D13 | Current problem status/links preserved; resolved items not unread; open-problem and notification-unread counters remain distinct. |
| D14 | Existing #425 Owner recovery completion/failure in-app and email delivery retained. |
| D15 | No granular notification preferences; SMTP configured only through deployment environment. |
| D16 | Mobile cards/menu/dialogs, state/problem/remedy/recovery; legacy offline data cleanup only, no SW registration, offline cache or PWA revival. |
| D17 | Sidebar estimate/delta and General per-Vault estimate; unavailable baseline explicit; AWS list-price caveats, including historical USD→EUR 1:1 convention; no budget/bill claims. |

## Validation and CI evidence

Local commands are targeted only:

- `.venv/bin/python -m unittest tests.test_essential_interface -v` — scope fail-closed/auth/schema/search/deep-link/SSE and recursive-estimate regressions.
- Focused Vitest modules for browser, Details, recovery, active work, scoped client, parity, Move/release/Stop, home, badges/theme/cleanup/create; see `vitest-final-focused.log` and `unittest.log` in this session.
- `.venv/bin/python scripts/export_openapi.py frontend/openapi.json`, `npm run generate:api`, `git diff --check`.

CI-only PostgreSQL regressions cover duplicate paths/paging/source-aware filters, sole-copy preservation, two-client scoped configuration/CSRF and recursive estimates including released missing Local Copies. CI-only Playwright covers creation, problem list/deep-link/remedy, release/Advanced, global Cloud-only search, file recovery admission and folder recovery preview, kit/backup safeguards, online-site upgrade and 375/1280 accessibility. No local browser/build/database/S3 verification is claimed.

Final exact-HEAD check conclusions/run URLs and counts are delivered in the coordinator response and `final-report.md` / `final-checks.json` session artifacts after CI settles. Earlier failed/cancelled runs are diagnostic only, never final evidence.

## Deployment caveats / limitations

- No deploy/restart performed. Build/deploy the coordinator-approved commit normally; API namespace changes and generated SPA must ship together. No new schema migration.
- Keep existing Owner auth/CSRF/reauth, reverse proxy, SMTP, S3 and PostgreSQL environment. Set `APP_PUBLIC_URL` to the canonical trusted application URL for useful email links. SMTP/address remain environment-owned rather than runtime-editable.
- Estimate is catalogued available-copy storage run-rate using bundled AWS region prices, not actual AWS bill or calendar-month usage; previous-month baseline may be absent. Existing EUR-labelled values use USD list figures 1:1, disclosed in UI. Requests/minimum charges/restored storage/early removal can add cost.
- CI fixtures do not prove delivery through Paolo's SMTP/Pocket ID/proxy, actual AWS retrieval times/cost, or recovery of real production objects. No claims of such live testing.
- A deliberately failing disposable S3 endpoint in browser fixtures tests recovery admission/dialog safety, not a successful real cold-object download. Existing MinIO/transfer CI owns actual storage/transfer integrity. Folder E2E confirms preview before cancellation; backend CI owns directory admission/estimation.
- No previews, persisted file listings, offline access or metadata photo search. Last-used Vault is browser/Owner-local, not a cross-device preference.
