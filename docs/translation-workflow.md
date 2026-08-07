# Translation workflow

This project is English-first. The UI defaults to English and ships a complete
Italian catalog. Persisted Job/audit state uses stable keys, not localized
prose.

## Catalogs

- Source of truth: `app/locales/en.json` (English) and `app/locales/it.json`
  (Italian).
- Lookup API: `app.i18n.translate(key, locale=..., **params)`.
- Critical keys that must exist in every locale live in
  `app.i18n.CRITICAL_KEYS`.

## Adding or changing a string

1. Choose a stable key such as `ui.sign_out`, `job.upload_verified`, or
   `api.scan_started`. Prefer domain terms from `CONTEXT.md` (Vault, Local Copy,
   Archive Version, Job, …).
2. Add the English string to `app/locales/en.json`.
3. Add the Italian string to `app/locales/it.json` in the same change.
4. If the string is on a critical path (login, archive chrome, Job progress,
   cancel/stop, or email subject/body), add the key to `CRITICAL_KEYS`.
5. Reference the key from code:
   - Python/API: `translate("…", locale=…)` or `set_job(..., message_key="…")`
   - React SPA: `t('…')` via the i18n provider (catalog from `/api/i18n/catalog`)
6. Run:

   ```bash
   .venv/bin/python -m unittest tests.test_i18n tests.test_locale_http -v
   cd frontend && npm run test
   ```

   `tests.test_i18n.CatalogIntegrityTests` fails on missing/unused critical keys
   and on EN/IT key-set drift.

## Locale preference

- Cookie: `frostvault_locale` (`en` or `it`).
- Authenticated update: `PUT /api/locale` with CSRF.
- Catalog fetch: `GET /api/i18n/catalog?locale=it`.

## Notifications and email

SMTP delivery is owned by issue #16. Define subjects/bodies as `email.*` keys
now and render them with `app.i18n.render_email(...)` so later delivery code
does not persist localized prose.
