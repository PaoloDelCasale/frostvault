# ADR 0006: English-default UI with stable message keys

## Status

Accepted

## Context

Issue #14 requires a public English-first repository while keeping a complete
Italian user experience. Job progress text and API flash messages previously
stored localized English prose in durable state, which prevents language
switching for historical Jobs and couples persistence to presentation.

## Decision

- Ship English as the default UI locale and Italian as a complete peer catalog
  under `app/locales/{en,it}.json`.
- Persist Job progress with stable `message_key` + JSON `message_params`, and
  keep an English `message` column only as a human-readable fallback/log aid.
- Localize at render time: HTML via Jinja `t()`, JSON APIs via
  `app.i18n.translate` / `present_job_message`, and browser chrome via an
  embedded catalog consumed by `t()` in static JS.
- Store the user preference in the `frostvault_locale` cookie (readable by JS for the
  login page switcher). Accept-Language is a secondary hint when no cookie
  exists.
- Keep audit `event` names and governance/OIDC `reason` codes as stable
  identifiers; never localize those persisted fields.
- Treat notification/email bodies as keyed templates (`email.*`) so issue #16
  can deliver SMTP/in-app copy without storing localized prose.
- Reject assembling security-sensitive notices through unescaped `innerHTML`
  interpolation; use `textContent` or `escapeHtml`.

## Consequences

- Catalog parity and critical-key coverage are enforced by unit tests.
- Legacy Job rows without `message_key` continue to display their stored prose.
- Contributors add keys to both locale files and update `CRITICAL_KEYS` when a
  string is user-visible on a critical path (see `docs/translation-workflow.md`).
