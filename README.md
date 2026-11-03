# FrostVault

FrostVault is a self-hosted web application for cataloging files, archiving
versioned copies in Amazon S3 storage classes, verifying uploaded content, safely
freeing local space, and recovering exact versions. It is a personal
archive with a single owner, isolated plaintext vaults, OIDC, and PostgreSQL.

The archive works like a file manager: it shows top-level folders, lets you
open them, and provides breadcrumbs for navigating back to parent folders.
Search remains global across every file in the selected vault.

## Owner and vaults

- FrostVault has exactly one **Owner**, who owns every vault. There are no other
  users, roles, members, invites, or shared vaults.
- The Owner signs in with OIDC (for example Pocket ID), configured only through
  the `OIDC_*` environment variables. The first successful OIDC login binds the
  provider subject to the Owner; any other subject is refused.
- The **Emergency Password** from `BOOTSTRAP_ADMIN_*` is accepted only from
  loopback and the LAN networks listed in `BREAK_GLASS_ALLOWED_CIDRS`; it is
  refused from anywhere else. An empty value means loopback-only (`127.0.0.0/8`
  and `::1`); there is no allow-all value.
- Reauthentication is required only before downloading the Recovery Kit and
  before deleting from the cloud.
- Each Vault has its own local folder and immutable S3 prefix.
- Every query and operation includes the vault identifier.
- Queued or active uploads, recoveries, and local cleanup operations can be
  stopped with the **Stop** button shown next to their progress.
- Operations run in parallel. `OPERATION_CONCURRENCY` controls the number of
  simultaneous processes (default: `4`, maximum: `16`). The previous
  `UPLOAD_CONCURRENCY` variable remains supported as a fallback.
- Local cleanup runs in the background and reports progress for each file or
  folder.
- Vaults are created from the UI with one cloud behavior: **Current Snapshot**
  keeps the last verified content of each file. A successful upload or safe
  rename verifies the new copy before destroying previous cloud bytes; a failed
  upload/verification preserves the previous copy. Recovery has no version picker.
  The shared S3 bucket stays versioned as an internal safety mechanism, not a
  recoverable history. Mistaken overwrites or ransomware can therefore replace
  the only good cloud copy after verification.
- A file whose Local Copy is gone (freed or deleted via SMB) keeps its verified
  cloud copy as the only copy. If **different** content later appears at the
  same path, FrostVault does not upload it: the file shows a conflict with a
  file kept only in the cloud, and an immediate notification (in-app, plus
  email when SMTP is configured) explains it. Rename the new file to protect
  it at its new path, or confirm the replacement with
  `POST /api/files/cloud-only-conflict/replace`: the new
  content is uploaded and verified before the previous cloud copy is deleted.
  Identical content is never a conflict; it becomes the Local Copy again
  without an upload.
- New vaults can use an empty managed root or adopt any existing directory
  under a Source Volume that does not overlap another vault root.
- Administration retains Vault, Source Volume and metadata backup operations;
  all installation settings come only from the deployment environment.

## Automatic protection

Browsing and statistics use five **protection states**, not copy-presence labels:

- **Pending**: new or changed local content not yet verified. A retained verified
  previous snapshot is indicated separately; it does not protect the new bytes.
- **Protected**: the regular Local Copy's fingerprint and matched Archive Version
  agree, with `integrity=verified` and `availability=available`.
- **Problem**: terminal upload failure, mismatched content verification, missing
  Archive Version, unsupported local entry, or a held Cloud-only Conflict.
- **Unconfirmed**: the Source Volume/relocation gate prevents observation or cloud
  requests cannot confirm availability. Last catalog evidence and its observation
  date remain visible; absence of a volume never turns files into Cloud only.
- **Cloud only**: no Local Copy is present and the retained cloud snapshot is
  verified and available. Recovery progress is independent of protection state.

Folders show counts for all five states; Vault statistics use the same derivation.
The aggregate headline prioritizes Problem, Unconfirmed, Pending, Protected, then
Cloud only (empty Vaults have no headline state). **Protection suspended** is a
separate per-Vault flag: pausing uploads does not invalidate already verified
content. None of these display states authorizes deleting a Local Copy or cloud
content; operation-specific safety checks remain mandatory.
Migration `0055_protection_states` rebuilds disposable directory projections only;
it changes no file, cloud object, fingerprint or verification evidence.

New Vaults enable automatic upload by default. The per-Vault **Enable automatic
upload** switch (or `PUT /api/vault/operation-policy`, `auto_upload`) pauses or
resumes it. Pausing holds queued automatic uploads; an already uploaded Archive
Version still finishes verification. Explicit manual uploads remain available.
Migration `0054_watcher_auto_upload` preserves both explicit pauses and the old
implicit pause of existing Vaults with no policy row; enable those deliberately.
It changes no cloud content and performs no S3 calls.

Filesystem watcher observations are the upload input. After five minutes without
changes, an independent scheduler admits them to the durable Job queue (checked
every ten seconds), without waiting for the default six-hour full scan. Both
mtime and the last observed content change must be old enough, so copying a file
with a preserved old timestamp does not bypass stability. Edits restart the
window; unchanged reconciliation scans and restarts do not. Full scans remain
the reconciliation net for events missed while stopped or with the watcher off.
With adequate bandwidth and a healthy provider, the target is verified protection
within about one hour of finishing the copy, not a transfer-time guarantee.

Automatic uploads always exclude `.DS_Store`, `Thumbs.db`, `._*`, `*.lock`,
`*-wal`, and `*-journal` at any path depth. These fixed exclusions cannot be
removed or overridden by include patterns. Existing additional include/exclude
rules still narrow eligibility; explicit manual operations are unaffected.

Enqueueing performs no cloud writes and does not depend on cloud validation or
storage-plan/lifecycle synchronization succeeding. Those failures remain visible
in scan health/errors. The upload worker still requires successful bucket
preflight before writing, honors retries and leases, and verifies the exact S3
VersionId with a full SHA-256 readback. Terminal automatic failures/cancellations
are not silently requeued for the same observation; retry manually or make a new
content change. Missing/replaced Source Volumes or Vault roots suspend local work
without clearing the catalog. SMB deletion never propagates to cloud deletion,
and Cloud-only Conflicts remain held back.

## Archive lifecycle and cloud deletion

- Each Vault has one **storage plan** in **Advanced**: Always accessible
  (Standard), Economical instant access (IA30 → GlacierIR90), Tiered archive
  (recommended: IA30 → GlacierIR90 → DeepArchive365), Early Deep Archive (day 0),
  or Custom (ordered object-age/class chain). Each explains timing and cost.
  There are no folder assignments, pins or manual per-file class changes.
- Plans emit only current-version transitions, never expiration or noncurrent
  rules. AWS requires age 30 for IA and minimum residence in the source class
  before the next step (30 days in IA, 90 in Glacier Instant/Flexible Retrieval),
  not 90/180 days from Standard before archiving. Objects below 128 KB stay put
  by default. Early replacement can incur minimum-duration charges; cold recovery
  takes hours. A changed plan does not warm existing cold objects or move them
  immediately. See [storage plans and upgrade safety](docs/aws-s3-bucket.md#vault-storage-plans).
- Lifecycle synchronization preserves external bucket rules and the seven-day
  incomplete-multipart abort rule. Changing a plan never deletes cloud content.
- **Purge from cloud permanently** deletes every selected Archive Version and
  Delete Marker after a confirmed, cancellable delay.
  A recently reauthenticated Owner can skip the remaining delay with
  **Delete now**.
- Cloud actions work on files and folders. Permanent folder and vault purges
  batch S3 deletions while preserving per-item failures for safe retries.
- Safe renames are recognized automatically only for unique, identical content
  observed within the scan window. The new cloud key is verified before cleaning
  up the old key (permanently under Current Snapshot). Ambiguous matches stay new
  files; no cloud content is deleted on their behalf. Path History is internal,
  not a UI action, and there is no manual rename confirmation.
- Archive statistics, file-state badges, and Job progress refresh automatically
  while background operations are active.

## Local cleanup safety

- The **Free local space** button appears only when the Local Copy fingerprint
  matches an available, verified Archive Version and when
  `ALLOW_LOCAL_DELETE=true`.
- Before deleting the server copy, the application checks the exact S3
  `VersionId`, atomically claims the local file, and rehashes it; object
  existence at the same key or a stale local fingerprint is not sufficient.
- Cleanup deletes local copies only. It never deletes the S3 object.
- Recovery downloads the exact archived S3 `VersionId`, verifies the plaintext
  SHA-256 digest, then atomically replaces the local file. Glacier/Deep Archive
  versions request a restore first (Bulk by default); high-impact restores are
  held for one hour awaiting the Owner's approval before any `RestoreObject`
  call. An accepted S3 restore request cannot be cancelled. Large recoveries
  use parallel S3 range downloads (`S3_DOWNLOAD_*`).
- The panel requires a username and password.

## Requirements

- Docker Engine with Docker Compose v2 for the recommended deployment, or
  Python 3.12+ for native development
- An S3 bucket with versioning and all four Block Public Access flags enabled,
  plus a least-privilege IAM principal (including `s3:GetBucketPublicAccessBlock`)
- PostgreSQL 16 for production, local development and tests (the only backend)
- PostgreSQL 16 client tools for native metadata backups and restore verification
- An HTTPS reverse proxy for any network-accessible deployment

FrostVault never replaces independent backups. Keep encrypted metadata backups
and custody of `ARCHIVE_MASTER_KEY` outside this server. The key protects only
metadata backups, not Vault content. Any authorized S3 tool can recover Vault
files; AWS SSE-S3 and HTTPS remain required protections.

## Setup

### Compose identity and fresh host data directory

The published image bakes in the default Unraid account `archive` (`99:100`).
It does **not** create users or groups when it starts. Both Compose manifests
instead launch the process directly as the numeric
`user: "${PUID:-99}:${PGID:-100}"` identity, so non-default numeric overrides
need no matching account in the image. This preserves the read-only root
filesystem, `cap_drop: [ALL]`, and `no-new-privileges` contract.

Before the first Compose start, create the repository's `./data` bind source on
the host with the identity selected in `.env`. A missing bind source may be
created by Docker as root, which prevents the non-root service from creating
its migration scratch and encrypted backup files:

```bash
# Use the same values in .env. These are the Unraid defaults.
# Omit `sudo` when your shell is already root, as is typical on Unraid.
PUID=99
PGID=100
mkdir -p ./data
sudo chown "${PUID}:${PGID}" ./data
sudo chmod 0750 ./data
```

This is a one-time fresh-directory preflight, not a recursive ownership reset
of an existing catalog. It deliberately changes only `./data`: prepare
`SOURCES_ROOT` under its own access policy and do not use it as a workaround for
source permissions. Full Linux, Unraid, Docker Desktop, and invalid-identity
guidance is in [docs/filesystem-permissions.md](docs/filesystem-permissions.md).

### Local development with PostgreSQL

The local configuration uses:

- a dedicated PostgreSQL 16 catalog (never a production database);
- a test source at `local-data/sources/test`;
- a real AWS bucket restricted to the `development/test` prefix;
- credentials read only from the Git-ignored `.env` file.

Local setup:

1. Provision a disposable PostgreSQL 16 server and database. For native/cloud
   development without Docker, follow the isolated `initdb` / `pg_ctl` procedure
   in [AGENTS.md](AGENTS.md). Use a user-owned cluster, not a system service.
2. Copy `.env.local.example` to `.env` if absent; configure `PGHOST`, `PGPORT`,
   `PGDATABASE`, `PGUSER` and database authentication. For cloud tests also set:

   ```text
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   S3_BUCKET=...
   ```

3. Never send AWS keys in chat or commit the `.env` file.
4. Use dedicated IAM credentials restricted to the test bucket/prefix, without
   delete permissions when cloud deletion is not being tested. Never use account
   root credentials.
5. Run the [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory),
   then pull the published image and start the application (schema migrations
   run automatically on start when `AUTO_MIGRATE=1`, the default):

   ```bash
   docker compose pull
   docker compose up -d
   ```

   Compose uses `ghcr.io/paolodelcasale/frostvault:latest`. Developers who need
   to change the image can still build from the `Dockerfile` locally.
   Set `AUTO_MIGRATE=0` to keep upgrades manual (then run
   `docker compose run --rm frostvault python -m app.backup_upgrade` before
   `up`).
While `REPLACE...` placeholder values remain, the application blocks AWS calls
instead of trying other credentials available on the computer.

### Production with PostgreSQL

1. Create the PostgreSQL database and user shown below.
2. Mount each Source Volume directly under the fixed `/sources` namespace.
   `managed` is reserved for server-provisioned empty Vault roots. Nested
   mounts are unsupported — every filesystem must be a sibling under
   `/sources/<alias>`:

   ```text
   /srv/frostvault/sources/          # host path bound to /sources
   ├── managed/                      # created by FrostVault (do not mount over)
   ├── photos/                       # direct host mount -> /sources/photos
   └── documents/                    # direct host mount -> /sources/documents
   ```

   Compose may still substitute the host path with `SOURCES_ROOT`; the
   application no longer accepts `VAULT_SOURCES_ROOT`.

   On first discovery FrostVault stores a markerless, opaque identity for each
   custom Source Volume. It accepts ordinary remounts of the same source but
   blocks scans, watchers, and all local operations if the alias is absent,
   inaccessible, ambiguous, unsupported, or backed by a different source. The
   catalog remains intact. There is no “accept replacement” action: restore the
   expected Compose mount. Bind, Docker Desktop, and NAS details and limitations
   are documented in [docs/filesystem-permissions.md](docs/filesystem-permissions.md).

   When creating a Vault, the Owner can keep the default empty root
   (`/sources/managed/<uuid>`) or adopt any existing directory under a Source
   Volume in place, provided it does not overlap another Vault root. Adoption never moves or rewrites content;
   the server still mints the Vault UUID and `vaults/<uuid>/` S3 prefix, then
   starts an asynchronous local scan.

3. Copy `.env.example` to `.env` and configure paths, bucket, credentials, and
   the Owner's Emergency Password (`BOOTSTRAP_ADMIN_*`). Before the first Compose start, run the
   [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory)
   using the same `PUID`/`PGID` values. For a network deployment set `COOKIE_SECURE=true`
   and provide `ALLOWED_HOSTS` (the hostnames the panel answers to) and
   `TRUSTED_PROXIES` (CIDRs of your reverse proxies); the app refuses to start
   in production without them. Set `OIDC_ENABLED=true` and configure
   `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` for single sign-on; OIDC
   is configured only through these variables. List your LAN in
   `BREAK_GLASS_ALLOWED_CIDRS` to accept the Emergency Password from it.

4. Set `VAULT_S3_BUCKET` and `AWS_DEFAULT_REGION` to the versioned, private bucket.
5. Configure `ARCHIVE_MASTER_KEY` for encrypted metadata backups and keep it
   outside this server; see [metadata backup custody](docs/metadata-backups.md).

Example database creation, run as a PostgreSQL administrator:

```sql
CREATE USER frostvault WITH PASSWORD 'A-LONG-PASSWORD';
CREATE DATABASE frostvault OWNER frostvault;
```

Schema changes are Alembic migrations. On container/app start, `AUTO_MIGRATE=1`
(the default) brings the database to the expected revision: fresh databases run
`alembic upgrade head`; existing databases that are behind take an encrypted
pre-upgrade metadata backup first (see
[docs/metadata-backups.md](docs/metadata-backups.md)), then upgrade. A failed
backup with real backup configuration blocks startup. Set `AUTO_MIGRATE=0` for
CI or fully manual release procedures, then run
`docker compose run --rm frostvault python -m app.backup_upgrade` before
starting a new application release. The application still validates the current
revision after any automatic upgrade and refuses to serve on a stale schema.

The first Alembic revision adopts only a database that exactly matches the
current multi-user release; unknown or historical single-user schemas stop
without modification. Existing cloud catalog rows migrate as unverified and
cannot authorize local cleanup until a later read-back verification resolves
their exact S3 version.

Downgrade is supported only while the target legacy schema can represent all
stored data. Alembic refuses a downgrade that would discard multiple Archive
Versions, Path History, Delete Markers, digests, or verified state; restore a
database backup instead.

If PostgreSQL runs directly on the server or exposes port `5432`, keep
`PGHOST=host.docker.internal`. Otherwise, connect both containers to the same
Docker network and set `PGHOST` to the PostgreSQL service name.

All Vaults store readable object keys `vaults/<uuid>/<relative-path>` and original
file bytes, without client-side encryption or extension transformation. Managed
SDK uploads use multipart transfers, a server-validated CRC32 `FULL_OBJECT`
checksum, a shared optional upload bandwidth cap, and throttled progress. The
upload response supplies the VersionId; verification streams that exact version
and compares SHA-256 with the local upload snapshot. Recovery also pins VersionId.
Incomplete multipart uploads are aborted on failure/cancellation, with a seven-day
lifecycle safety net for process interruption. No external transfer binary or
remote configuration is required.

Migration `0051_plaintext_vaults` refuses legacy
encrypted Vaults before any schema change; recover test data using the previous
release before creating a fresh plaintext installation. It never rewrites or
deletes S3 objects. See [upgrade notes](docs/metadata-backups.md#plaintext-only-upgrade-0051).

## Run

After completing the [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory):

```bash
docker compose pull
docker compose up -d
docker compose logs -f frostvault
```

Schema migrations run automatically on start (`AUTO_MIGRATE=1` by default). Set
`AUTO_MIGRATE=0` and run `python -m app.backup_upgrade` yourself when you want
an explicit pre-upgrade gate outside the container entrypoint.

The service listens on `127.0.0.1:8080` by default. Use an HTTPS reverse proxy or
Tailscale to make it reachable. Set `COOKIE_SECURE=true` when using HTTPS.

Compose pulls `ghcr.io/paolodelcasale/frostvault:latest` from the GitHub
Container Registry (a standard deploy does not build the image locally).
For production behind Traefik, use `compose.traefik.yaml` so the app is not
published on host ports. See [docs/traefik.md](docs/traefik.md).

The image bakes in default `99:100`; Compose runs directly as numeric
`PUID`/`PGID` and never creates an account at runtime. Permission the host
source and data directories for that identity before start; see
[docs/filesystem-permissions.md](docs/filesystem-permissions.md) for the
required `./data` preflight and valid-override behavior.

On first startup, the application creates the Owner defined by
`BOOTSTRAP_ADMIN_*`; create vaults from the UI. The variables are re-applied at
every start, so they remain the Owner's Emergency Password: changing the
password revokes existing sessions. Removing them keeps the stored credentials.

## Localization

The UI defaults to English and includes a complete Italian translation. You
can switch language from the archive header or the sign-in page; the preference
is stored in the `frostvault_locale` cookie. Contributor guidance for catalogs and
critical-key checks lives in `docs/translation-workflow.md`.

## Continuous integration

Pull-request CI is deterministic and does not need AWS credentials: unit tests,
PostgreSQL migrations, frontend JS tests, and S3-compatible integrity proofs
against a local versioned S3 server.
A separate optional manual workflow uses GitHub OIDC against a prefix-scoped IAM
role for real AWS digest checks. See [docs/ci.md](docs/ci.md) for job status,
cleanup reruns, and security scanner severity gates.

## Recommended first smoke test

Before indexing a terabyte of data:

1. use a test source folder containing two unimportant files;
2. wait for the local catalog to pick them up through the filesystem watcher
   (or trigger an administrative `POST /api/scan` if you need a full
   reconciliation immediately);
3. upload one file and confirm that a distinct S3 version exists in the bucket;
4. confirm the catalog keeps the version unverified until plaintext read-back
   verification completes;
5. confirm **Free local space** and **Recover** remain unavailable for an
   unverified version;
6. keep the metadata backup master key and AWS access/recovery instructions in
   secure storage separate from the server.

The local catalog updates automatically when files under the configured source
are created, modified, renamed, or deleted. Native filesystem watching is the
primary mechanism; authenticated browsers receive a Vault-scoped catalog
revision signal and refresh the archive view without idle polling or a manual
**Refresh list** action. A low-frequency full scan (`SCAN_INTERVAL_SECONDS`,
default six hours) remains the reconciliation safety net for missed events,
startup, and mount-return correctness. Set
`FILESYSTEM_WATCH_FORCE_POLLING=true` with Docker Desktop on Windows. On a Linux
server, prefer the native watcher with the value `false`.
See [docs/catalog-events.md](docs/catalog-events.md) for the SSE journal cost
model and reconnect semantics.

## Indicative minimum AWS permissions

The application role/user must be able to list the bucket and, within the
configured prefix, read, upload, and request object restores. Automatic rename
cleanup requires `s3:DeleteObjectVersion` to remove the old key's versions
only after the new key is verified. The same permission removes superseded
versions after verified uploads; permanent purge uses it too.
Disabling the manual cloud-deletion setting does not disable verified snapshot
replacement or automatic rename cleanup.

Use the Terraform baseline in
[`infra/terraform/archive-bucket/`](infra/terraform/archive-bucket/) or apply
equivalent versioning, encryption, public-access blocking, ownership, and
least-privilege controls. See [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md) for
the full model.

## Configuration

Start from `.env.local.example` for local PostgreSQL use or `.env.example` for a
PostgreSQL deployment. Every value containing `REPLACE` is a placeholder and
must be replaced before the corresponding integration is used. Important groups:

| Area | Variables |
| --- | --- |
| Database | `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` (or `.pgpass`); optional PostgreSQL `DATABASE_URL` overrides corresponding `PG*` values |
| Authentication | `BOOTSTRAP_ADMIN_*` (the Owner and its Emergency Password), `OIDC_*`, `BREAK_GLASS_ALLOWED_CIDRS` (LAN networks that accept the Emergency Password) |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES` |
| Frontend | `FRONTEND_DIST_DIR` (Vite build output; required for HTML routes) |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `SMTP_TO_ADDRESS`, `SMTP_USE_TLS` (optional; see below) |
| Storage | `S3_BUCKET`, `VAULT_S3_BUCKET`, `AWS_DEFAULT_REGION` |
| Encryption and backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` |
| Operations | `OPERATION_CONCURRENCY`, `S3_DOWNLOAD_*`, `RESTORE_*`, `CLOUD_PURGE_DELAY_SECONDS`, `ALLOW_LOCAL_DELETE` |

All installation configuration is environment-only and takes effect on process
restart. There is no mutable settings UI or API. Before upgrading through
migration `0048_essential_administration`, move any required database overrides
into deployment environment variables; the migration discards those overrides.
See [ADR-0009](docs/adr/0009-effective-system-configuration.md).

There is no audit or worker-error page. Sensitive/destructive actions (including
cloud deletion, recovery-material downloads and root relocation) retain an
append-only, redacted action log. Worker errors appear as internal-error
problems in the notification center. The real scan/watcher reports every
unreadable path there; inaccessible roots block protection. No background
diagnostic walker or filesystem banner runs. `/metrics` retains process-local
Job, queue, worker, verification, notification delivery and backup signals,
without per-file labels or filesystem/listing performance diagnostics.

OIDC is configured only through the `OIDC_*` variables and binds the Owner on
the first login. See [ADR-0017](docs/adr/0017-single-owner.md).

### Notifications and optional email

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
startup, then at most once every seven days). It includes all five protection
state counts, per-Vault last successful scan/audit and suspension, the last
verified metadata backup (or an explicit unknown date), open problems, files and
space freed and blocked cleanups over the last seven days, and the estimated
monthly storage cost from bundled AWS list prices, not an AWS bill. No automatic
restore or external heartbeat is introduced: a missing weekly email can mean
that the application/server or SMTP is down.

Email is optional and configured only in the deployment environment:

- `SMTP_HOST`, `SMTP_FROM_ADDRESS`, `SMTP_TO_ADDRESS` — all three must be
  non-empty to enable delivery. The recipient is explicitly configured, not
  inferred from the Owner's login address.
- `SMTP_PORT` — defaults to `587`; valid range `1–65535`.
- `SMTP_USERNAME`, `SMTP_PASSWORD` — optional authentication. The password is
  never exposed by an API or stored in the database.
- `SMTP_USE_TLS` — defaults to `true`: STARTTLS on port 587 (and other ports),
  implicit TLS on port 465. Disable only for a trusted local relay.

Missing or partial configuration disables email, not the inbox. The current
General-compatible administration surface warns when email is not configured;
the General page layout is delivered separately. The API exposes only a boolean
configuration status, never SMTP addresses or credentials. Email delivery has
three bounded attempts and does not change Job results or remove notifications.
Environment changes require an application restart. Existing database SMTP
configuration is discarded by migration `0046_simplify_notifications`; set the
SMTP environment variables before upgrading if you want email to continue.
Migration `0056_notification_problems` adds notification problem state, continuous
pending-file timers and suspension timestamps, and removes obsolete routine
completion/cleanup inbox history and its deliveries. It does not change Jobs,
Local Copies, Archive Version evidence or cloud content.

### Online responsive site

Use the responsive site on desktop or phone to check status, resolve problems
and launch recoveries. A network connection is required: FrostVault does not
install a PWA, register a service worker, or persist file listings in the browser.
Authenticated API responses are `no-store`.

When upgrading from a PWA release, the site removes its old browser caches and
registration without clearing theme preferences. `/sw.js` remains only as an
unregistering retirement script so existing installations stop serving the old
shell; the current frontend never registers it. Migration
`0047_remove_offline_cache` removes the Session cache nonce, preserving existing
Sessions and their internal concurrency lock. No file or cloud data is changed.

Do not commit `.env`, master-key material, database files, or
cloud credentials.

## Security

- Expose FrostVault only through HTTPS and configure trusted hosts and proxies.
- Prefer OIDC and short-lived AWS credentials. Accept the Emergency Password only
  from loopback and your LAN;
  an empty `BREAK_GLASS_ALLOWED_CIDRS` value is loopback-only, never allow-all.
- Grant only the documented S3 actions and prefixes. Do not grant account-root
  credentials or broad bucket deletion rights.
- Keep versioning enabled, test restores, and review problems in the notification center.
- Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Development and tests

Create a virtual environment, install the pinned dependencies, and run the
database migration before starting the app:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# PostgreSQL 16 must already be running (native setup: AGENTS.md).
# .env is not auto-loaded; keep credentials in this Git-ignored file.
set -a; . ./.env; set +a
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

On Windows PowerShell, use `.venv\Scripts\python.exe` and set environment
variables with `$env:NAME = "value"`.

### Frontend

The UI is a React SPA. The container image builds `frontend/dist` in a Node
stage; uvicorn always serves it for HTML routes (hashed assets are cached
immutably; `index.html` is `no-store`). For a native run, build first:
`cd frontend && npm ci && npm run build`.

For day-to-day SPA work, run uvicorn and the Vite dev server together. The Vite
proxy forwards `/api`, `/auth`, and `/login` to `http://127.0.0.1:8080` with
`changeOrigin: true` so the Host FastAPI sees is `127.0.0.1` (see ADR-0005).
Leave `ALLOWED_HOSTS` empty locally, or list `127.0.0.1`; a wrong Host yields
hard-to-diagnose login failures.

```bash
# terminal 1
set -a; . ./.env; set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

# terminal 2
cd frontend && npm ci && npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). To exercise the production
serving path without Docker: `cd frontend && npm run build`, then start uvicorn
and open `http://127.0.0.1:8080`.

Open a draft PR early and use its CI for complete Python, PostgreSQL/migration,
S3, Playwright, frontend and image verification (#410). Locally run only targeted
modules, for example:

```bash
.venv/bin/python -m unittest tests.test_database.DatabaseMigrationTests.test_fresh_postgresql_database_migrates_to_a_startable_schema -v
```

All database tests require `TEST_POSTGRES_URL` pointing at an **isolated**
PostgreSQL 16 server with a CREATEDB role. They create uniquely named databases
and clone a real migrated HEAD template for fast empty fixtures; populated and
pinned migration cases still execute Alembic. Missing configuration fails rather
than skipping database regressions. S3-compatible cases additionally require
`TEST_S3_ENDPOINT`. See [docs/ci.md](docs/ci.md).

## Known limits

- FrostVault is a self-hosted single-application deployment, not a managed
  multi-region service.
- AWS operations depend on external service availability and may
  incur storage, retrieval, request, and transfer charges.
- Glacier restore requests cannot be cancelled after S3 accepts them.
- Legacy SQLite installations/backups require the previous release for recovery;
  there is no automatic conversion to PostgreSQL. See [metadata backups](docs/metadata-backups.md#postgresql-only-release-422).
- The application protects archive workflows but does not replace offline,
  independently tested disaster-recovery copies.

## License

Licensed under the [Apache License 2.0](LICENSE).
