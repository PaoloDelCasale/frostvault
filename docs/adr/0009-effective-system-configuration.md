# Effective system configuration ownership

## Decision

FrostVault keeps one allow-listed configuration catalog in
`app/system_settings.py`. Each entry defines its stable key, environment
variable, JSON type, built-in default, API group, mutability, restart behavior,
and secret status. New environment variables are not implicitly settings and
cannot be persisted without a catalog and schema migration change.

The effective value of a runtime-managed key is resolved centrally in this
order:

1. database override;
2. environment default;
3. built-in default.

The frozen deployment `Settings` object remains the environment/default input.
Consequently, installations with no rows in `system_settings` behave exactly as
before. Code that needs an effective managed value must use the resolver rather
than reproduce this precedence.

### Lifecycle and trust labels

- `deployment_only`: owned by the operator because it establishes a trust,
  storage, credential, bootstrap, or process boundary. It is read-only in the
  administration API.
- `runtime_managed`: may be stored as typed JSON through the bounded mutation
  interface, which rejects unknown keys, wrong JSON
  types, out-of-range values, invalid choices, and cross-field conflicts before
  persistence.
- `restart_required`: visible but read-only until a lifecycle-specific change
  mechanism exists; changing the deployment environment takes effect after
  restart.
- `secret`: an orthogonal trust label. Secret values are never serialized.
  APIs expose only whether a non-placeholder value is configured.

The complete classification is:

| Classification | Environment variables |
| --- | --- |
| Deployment-only, restart | `DB_BACKEND`, `SQLITE_PATH`, `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `SESSION_COOKIE_NAME`, `CSRF_COOKIE_NAME`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES`, `BREAK_GLASS_ALLOWED_CIDRS`, `RCLONE_CONFIG`, `AWS_DEFAULT_REGION`, `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_DISPLAY_NAME`, `BOOTSTRAP_VAULT_NAME`, `BOOTSTRAP_VAULT_SLUG`, `BOOTSTRAP_VAULT_SOURCE_ROOT`, `S3_BUCKET`, `BOOTSTRAP_VAULT_S3_PREFIX`, `BOOTSTRAP_VAULT_RCLONE_REMOTE`, `VAULT_SOURCES_ROOT`, `VAULT_S3_BUCKET`, `VAULT_RCLONE_REMOTE`, `VAULT_RCLONE_BASE_REMOTE`, `AUTO_MIGRATE`, `METADATA_BACKUP_DIR`, `FRONTEND_DIST_DIR`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` |
| Secret, deployment-only, restart | `PGPASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `OIDC_CLIENT_SECRET`, `BOOTSTRAP_ADMIN_PASSWORD`, `ARCHIVE_MASTER_KEY`, `VAPID_PRIVATE_KEY` |
| Restart-required | `SESSION_IDLE_SECONDS`, `SESSION_ABSOLUTE_SECONDS`, `FILESYSTEM_WATCH_ENABLED`, `FILESYSTEM_WATCH_FORCE_POLLING`, `OIDC_ENABLED`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_SCOPES`, `OIDC_LOGIN_TTL_SECONDS` |
| Runtime-managed | `REAUTH_WINDOW_SECONDS`, `SCAN_INTERVAL_SECONDS`, `AUDIT_INTERVAL_SECONDS`, `FILESYSTEM_WATCH_DEBOUNCE_MS`, `FILESYSTEM_WATCH_POLL_MS`, `QUEUE_POLL_SECONDS`, `OPERATION_CONCURRENCY` (legacy default alias `UPLOAD_CONCURRENCY`), `JOB_POLL_SECONDS`, `RESTORE_DAYS`, `RESTORE_TIER`, `RESTORE_HIGH_IMPACT_GIB`, `RESTORE_HIGH_IMPACT_EUR`, `RESTORE_APPROVAL_HOLD_SECONDS`, `CLOUD_PURGE_DELAY_SECONDS`, `ALLOW_LOCAL_DELETE`, `BANDWIDTH_LIMIT_KIBPS`, `S3_DOWNLOAD_MAX_CONCURRENCY`, `S3_DOWNLOAD_MULTIPART_THRESHOLD_MIB`, `S3_DOWNLOAD_MULTIPART_CHUNKSIZE_MIB`, `RCLONE_MULTI_THREAD_STREAMS`, `RCLONE_MULTI_THREAD_CUTOFF_MIB`, `JOB_PROGRESS_MIN_INTERVAL_MS`, `INVITE_TTL_SECONDS`, `METADATA_BACKUP_RETENTION`, `METADATA_BACKUP_INTERVAL_SECONDS`, `METADATA_BACKUP_S3_PREFIX`, `METADATA_BACKUP_VERIFY_INTERVAL_SECONDS` |

The deployment templates also use `SOURCES_ROOT`, `APP_PORT`, `PUID`, `PGID`,
and (for local placeholder credentials) `AWS_EC2_METADATA_DISABLED`. These are
deployment-only container/runtime controls, not application settings. They are
not visible inside the application when Compose substitutes them, so the API
must not invent an effective value for them.

Rclone credentials remain in the operator-owned file referenced by
`RCLONE_CONFIG`; the application neither imports nor exposes them. Database
credentials and paths, the archive master key, AWS/rclone credentials,
filesystem and mount roots, proxy/host/cookie trust, bootstrap inputs,
automatic migration behavior, and the frontend distribution path cannot be
edited because changing them can redirect data, bypass perimeter assumptions,
lock out administrators, or invalidate the running process.

## Reload, failure, and rollback

Runtime-managed consumers resolve at their documented lifecycle point. A
successful committed override is visible on the next resolution; settings
marked restart-required are loaded only when the process restarts. A write and
its audit event must share one transaction. Validation happens before the
write, so an invalid value changes nothing; any application or transaction
failure rolls back to the previous row. Removing an override deletes its row
and therefore falls back to the environment and then the built-in default.
Concurrent mutation and bounded-value semantics are defined by the follow-up
runtime-management interface.

### Runtime mutation interface

`GET /api/admin/settings` returns the grouped effective catalog and a global
integer `revision`. `PATCH /api/admin/settings` accepts that revision plus an
`overrides` object and a separate `removals` list. Keeping removals separate
preserves `null` as a legitimate value for nullable settings.

The mutation requires an administrator, CSRF protection, and recent
Reauthentication. SQLite obtains an immediate write lock; PostgreSQL obtains a
transaction-scoped advisory lock. The server compares the submitted revision
after taking the lock and returns `409 stale_system_settings` when another
mutation committed first.

Validation of the complete candidate configuration finishes before any row is
written. Setting rows and the append-only `system_settings.updated` audit event
share one transaction. The audit detail records each key's old and new value
and source. The count of committed settings audit events is the durable global
revision, so rollback leaves both the effective configuration and revision
unchanged.

Runtime consumers resolve one effective snapshot at their natural lifecycle
point: each HTTP request, Invite creation, Job batch or Job execution, watcher
start, and background scheduler iteration. This avoids process restart while
keeping a coherent set of values throughout one operation.

Metadata snapshots contain only effective, non-secret catalog entries. Secret
entries are structurally absent even when a caller supplies a custom snapshot.
The administrator read API follows the same catalog and returns only a
`configured` boolean for secrets.

## Consequences

Operators retain control of deployment trust boundaries. Administrators gain a
single truthful inventory with provenance without receiving credentials.
Adding a managed setting intentionally requires code, migration, validation,
and documentation changes instead of accepting arbitrary environment names.
