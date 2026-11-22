# Configuration

All installation configuration is environment-only. Start from `.env.example` (deployment) or `.env.local.example` (local development).

## Variables

Start from `.env.local.example` for local PostgreSQL use or `.env.example` for a
PostgreSQL deployment. Every value containing `REPLACE` is a placeholder and
must be replaced before the corresponding integration is used. Important groups:

| Area | Variables |
| --- | --- |
| Database | `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` (or `.pgpass`); optional PostgreSQL `DATABASE_URL` overrides corresponding `PG*` values |
| Authentication | `BOOTSTRAP_ADMIN_*` (the Owner and its Emergency Password), `OIDC_*`, `BREAK_GLASS_ALLOWED_CIDRS` (LAN networks that accept the Emergency Password) |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES`, `FORWARDED_ALLOW_IPS` (Uvicorn) |
| Frontend | `FRONTEND_DIST_DIR` (Vite build output; required for HTML routes) |
| Diagnostics | `API_DOCS_ENABLED` (default false; isolated development only), `METRICS_TOKEN` (default empty; independent Bearer scraper secret) |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `SMTP_TO_ADDRESS`, `SMTP_USE_TLS` (optional; see below); `TZ` (IANA zone such as `Europe/Rome` for notification dates; missing or invalid means UTC, labelled "UTC") |
| Storage | `AWS_DEFAULT_REGION` (the region of every new Vault Bucket; `VAULT_S3_BUCKET` and `S3_BUCKET` were removed in #735) |
| Encryption and backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` (optional `METADATA_BACKUP_BUCKET`: an operator-supplied System Bucket; empty lets FrostVault create `frostvault-system-<random>`) |
| Operations | `OPERATION_CONCURRENCY`, `S3_DOWNLOAD_*`, `RESTORE_*` |
| Scans | `SCAN_INTERVAL_SECONDS`, `SCAN_HASH_CONCURRENCY` (default 4, 1–32: Local Copies a full scan fingerprints at once; HDD arrays and FUSE unions such as Unraid `shfs` read faster with several reads queued, 1 restores one file at a time), `SOURCE_DEVICE_READ_CONCURRENCY` (1–16: reads in flight per Source Device, shared by scan verification and uploads; on an Unraid user share each array disk is a device; unset by default: 1 on a rotational disk and 3 on a solid-state or unknown one, from `/sys/block/<disk>/queue/rotational`; a value applies to every device), `SERVER_PRIORITY_IOWAIT_PERCENT` (empty or 0 = off: above this host iowait for 10 s, one read per device and no new uploads until 30 s below; see [ADR-0046](adr/0046-reads-per-source-device.md)) |

All installation configuration is environment-only and takes effect on process
restart. There is no mutable settings UI or API. See [ADR-0009](adr/0009-effective-system-configuration.md).

There is no audit or worker-error page. Sensitive/destructive actions (including
cloud deletion, Recovery Kit downloads and folder reconnects) retain an
append-only, redacted action log. Worker errors appear as internal-error
problems in the notification center. The real scan/watcher reports every
unreadable path there; inaccessible roots block protection. No background
diagnostic walker or filesystem banner runs. `/metrics` retains process-local
Job, queue, worker, verification, notification delivery and backup signals,
without per-file labels or filesystem/listing performance diagnostics, behind the
explicit Bearer opt-in described above. Queue stalls remain visible in protected
metrics and Owner problems, not the anonymous readiness payload.

Upload Remediation keeps one episode per file only while an ended upload left
cloud work open (an uncertain send, an unverified replacement, an open cleanup
or a reconciliation block); a successful upload writes nothing for it, so there
is no backlog to re-examine. `upload_remediation_due_queue` and
`upload_remediation_states` count open episodes. The sweep recreates a missing
cursor without resetting existing round-robin progress. Missing ticks or ticks
older than 50 seconds open an Owner problem; `upload_remediation_sweep_stale` and the oldest actual
`upload_remediation_last_tick_unixtime` support protected monitoring (zero means
at least one tick is missing). Receipt-journal holds are reported by
`upload_journal_held` and an Owner problem when the catalog is writable. Held
Jobs release their claims and wait without spending transfer retries. Once the
journal's INSERT/commit probe succeeds, unrelated uploads may continue; the
uncertain attempt keeps its token, snapshot and send-started fence and must still
reconcile before any send. No provider reads of another Vault are needed to
reopen admission. These are informational health signals, not new container
readiness dependencies.

OIDC is configured only through the `OIDC_*` variables and binds the Owner on
the first login. See [ADR-0017](adr/0017-single-owner.md).

## Notifications and optional email

The notification center uses fixed rules, with no per-event, per-channel or
per-Vault preferences. Immediate inbox alerts (and optional email) cover:

- **Protection delayed**, aggregated once per Vault and continuous problem episode:
  a stable queue with no completed verified upload for about two hours, or any
  included file continuously unprotected for 24 hours. Large progressing imports
  do not alert before that ceiling. Files still changing after 24 hours are counted
  as “not stabilizing”, not as upload errors. Timers survive application restarts.
- **Terminal upload failure**, after retries are exhausted or a permanent/mismatch
  failure, not every retry.
- **Protection blocked**, for an unavailable Source Volume, cloud preflight or
  configuration/policy failures, or automatic protection suspended for 24 hours.
- **Missing cloud copy** and **Cloud-only Conflict**.
- An Owner-requested **recovery completed or failed**, the sole routine completion
  exception. Successful uploads, verification and individual local cleanups do not
  create notifications.

Each problem shows its **current open/resolved state**. Reading is not resolution:
`open_problem_count` and `unread_count` are independent. Resolved problems are
marked read automatically, and pending email for them is not sent. A recurrence
reopens the problem and notifies once again.

A **weekly summary** is both an inbox item and optional email (the first runs on
startup, then at most once every seven days). It includes all six protection
state counts, per-Vault last successful scan/audit and suspension, the last
verified metadata backup (or an explicit unknown date), open problems, files and
space freed and blocked cleanups over the last seven days, and the estimated
monthly storage cost from bundled AWS list prices, not an AWS bill. Its dates
are local date and time in the `TZ` zone (UTC, labelled, without a valid `TZ`). No automatic
restore or external heartbeat is introduced: a missing weekly email can mean
that the application/server or SMTP is down.

Email is optional and configured only in the deployment environment:

- `SMTP_HOST`, `SMTP_FROM_ADDRESS`, `SMTP_TO_ADDRESS` — all three must be
  non-empty to enable delivery. The recipient is explicitly configured, not
  inferred from the Owner's login address.
- `APP_PUBLIC_URL` — optional canonical application URL (for example
  `https://frostvault.example.com`), without credentials/query/fragment. Enables
  email links to the file's folder, highlighted row and problem Details. Without
  it email still works but has no absolute application link.
- `SMTP_PORT` — defaults to `587`; valid range `1–65535`.
- `SMTP_USERNAME`, `SMTP_PASSWORD` — optional authentication. The password is
  never exposed by an API or stored in the database.
- `SMTP_USE_TLS` — defaults to `true`: STARTTLS on port 587 (and other ports),
  implicit TLS on port 465. Disable only for a trusted local relay.

Missing or partial configuration disables email, not the inbox. The System
page warns when email is not configured. The API exposes only a boolean
configuration status, never SMTP addresses or credentials. Email delivery has
three bounded attempts and does not change Job results or remove notifications.
Environment changes require an application restart.

## Online responsive site

Use the responsive site on desktop or phone to check status, resolve problems
and launch recoveries. A network connection is required: FrostVault does not
install a PWA, register a service worker, or persist file listings in the browser.
Authenticated API responses are `no-store`.

When upgrading from a PWA release, the site removes its old browser caches and
registration without clearing theme preferences. `/sw.js` remains only as an
unregistering retirement script so existing installations stop serving the old
shell; the current frontend never registers it.

Do not commit `.env`, master-key material, database files, or
cloud credentials.
