# Deployment and operations

Production deployment with Docker Compose, PostgreSQL and S3, plus the container healthcheck, HTTP diagnostics, graceful shutdown and AWS permissions. Configuration variables are listed in [configuration.md](configuration.md).

## Requirements

- Docker Engine with Docker Compose v2 for the recommended deployment, or
  Python 3.12+ for native development
- An S3 bucket with versioning and all four Block Public Access flags enabled,
  plus a least-privilege IAM principal (including `s3:GetBucketPublicAccessBlock`)
- PostgreSQL 16 for production, local development and tests (the only backend)
- PostgreSQL 16 client tools for native metadata backups and restore verification
- An HTTPS reverse proxy for any network-accessible deployment

FrostVault never replaces independent backups. Keep encrypted metadata backups
and the installation **Recovery Kit** outside this server. Download requires
recent reauthentication; compare the visible fingerprint and confirm custody
once for the current kit. It contains the metadata key, cloud location and
manual PostgreSQL/S3 instructions, not catalog data or AWS credentials. The key
protects metadata backups only. Any authorized S3 tool can recover Vault files;
AWS SSE-S3 and HTTPS remain required protections. After metadata restore,
Reconnect Folder for each Vault and review the durable backup-date/updated-files
warning. See [manual disaster recovery](metadata-backups.md).

## Compose identity and fresh host data directory

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
guidance is in [docs/filesystem-permissions.md](filesystem-permissions.md).

`PUID=0 PGID=0` runs FrostVault as root **without** root privileges, for shares
written by containers running as root (for example the Immich library): keep
`cap_drop: [ALL]` and `no-new-privileges`, and add back only `CHOWN` so a
Recovery restores the original owner and mode. The entrypoint refuses to start
as root with any capability that bypasses file permissions. See
[Running as root](filesystem-permissions.md#running-as-root-puid0).

## Production with PostgreSQL

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
   application no longer accepts `VAULT_SOURCES_ROOT`. A bind of a folder on
   the same filesystem as `/sources` is a valid Source Volume; a direct child
   that is not a mount degrades only that alias, not the whole instance.

   On first discovery FrostVault stores a markerless, opaque identity for each
   custom Source Volume. It accepts ordinary remounts of the same source but
   blocks scans, watchers, and all local operations if the alias is absent,
   inaccessible, ambiguous, unsupported, or backed by a different source. The
   catalog remains intact. Restore the expected Compose mount or explicitly
   **Reconnect Folder** for one existing Vault: accept a new volume/folder,
   reconcile S3 without deletions, then complete a full local rehash. Matching
   files become Protected, missing files Cloud-only and different content stays
   held as a conflict. Sibling Vaults are never implicitly accepted. Bind, Docker
   Desktop, and NAS details and limitations
   are documented in [docs/filesystem-permissions.md](filesystem-permissions.md).

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
   `TRUSTED_PROXIES` (the app's proxy IP/CIDR allowlist); the app refuses to start
   in production without them. **Also set `FORWARDED_ALLOW_IPS` explicitly** for
   Uvicorn to the same narrow proxy IPs/CIDRs as seen by FrostVault. Neither list
   derives from the other. Uvicorn rewrites both scheme and client address;
   `TRUSTED_PROXIES` cannot veto overly broad Uvicorn trust. Without the proxy in
   `FORWARDED_ALLOW_IPS`, OIDC redirects remain `http` even with secure cookies.
   Never use `*` or an entire Docker/LAN range as a shortcut; follow the
   [Traefik trust and verification guide](traefik.md).
   Set `OIDC_ENABLED=true` and configure
   `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` for single sign-on; OIDC
   is configured only through these variables. The optional `OIDC_PROVIDER_NAME`
   names the provider on the sign-in button (for example `Pocket ID`). List your LAN in
   `BREAK_GLASS_ALLOWED_CIDRS` to accept the Emergency Password from it.

4. Set `AWS_DEFAULT_REGION` and AWS credentials with the
   [FrostVault buckets policy](../infra/terraform/archive-bucket/iam/frostvault-buckets-policy.json):
   FrostVault creates a private, versioned **Vault Bucket** for every new Vault
   (`frostvault-<name>-<random>`, see
   [buckets FrostVault creates](aws-s3-bucket.md#buckets-frostvault-creates)).
   There is no bucket setting; `VAULT_S3_BUCKET` was removed in #735.
5. Configure `ARCHIVE_MASTER_KEY` for encrypted metadata backups and keep it
   outside this server; see [metadata backup custody](metadata-backups.md).
   The backups go to the System Bucket FrostVault creates
   (`frostvault-system-<random>`), or to the bucket named by the optional
   `METADATA_BACKUP_BUCKET`; see [System Bucket](metadata-backups.md#system-bucket).

Example database creation, run as a PostgreSQL administrator:

```sql
CREATE USER frostvault WITH PASSWORD 'A-LONG-PASSWORD';
CREATE DATABASE frostvault OWNER frostvault;
```

The v0.6 schema starts at the Alembic revision `0001_baseline`
([ADR-0035](adr/0035-database-rebuilt-from-scratch.md)), followed by
linear migrations ([ADR-0041](adr/0041-linear-migrations-after-the-frozen-baseline.md)).
On every start the application checks the database before any write: an
**empty** database is initialized with `alembic upgrade head`; a database at an
**earlier v0.6+ revision** is upgraded in place; a database at this release's
revision is used; any other database (a v0.5.x revision, a newer release's
revision, or tables without `alembic_version`) is **refused** with a clear
message. FrostVault never stamps, resets or adopts a v0.5.x catalog, and
refuses its metadata backups: v0.6 starts from an empty database and new Vaults
use new cloud namespaces. Keep the previous release to read an older catalog or
backup.

If PostgreSQL runs directly on the server or exposes port `5432`, keep
`PGHOST=host.docker.internal`. Otherwise, connect both containers to the same
Docker network and set `PGHOST` to the PostgreSQL service name.

## Run

After completing the [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory):

```bash
docker compose pull
docker compose up -d
docker compose logs -f frostvault
```

On start an empty database is initialized, one of an earlier v0.6+ release is
upgraded, and any other database is refused before any write (see
[ADR-0035](adr/0035-database-rebuilt-from-scratch.md) and
[ADR-0041](adr/0041-linear-migrations-after-the-frozen-baseline.md)).

The service listens on `127.0.0.1:8080` by default. Use an HTTPS reverse proxy or
Tailscale to make it reachable. Set `COOKIE_SECURE=true` when using HTTPS.

Compose pulls `ghcr.io/paolodelcasale/frostvault:latest` from the GitHub
Container Registry (a standard deploy does not build the image locally).
For production behind Traefik, use `compose.traefik.yaml` so the app is not
published on host ports. It requires both `TRUSTED_PROXIES` (app) and
`FORWARDED_ALLOW_IPS` (Uvicorn), explicitly configured to the actual trusted
proxy addresses, with no permissive defaults. See
[docs/traefik.md](traefik.md) for topology selection and HTTPS OIDC checks.

The image bakes in default `99:100`; Compose runs directly as numeric
`PUID`/`PGID` and never creates an account at runtime. Permission the host
source and data directories for that identity before start; see
[docs/filesystem-permissions.md](filesystem-permissions.md) for the
required `./data` preflight and valid-override behavior.

On first startup, the application creates the Owner defined by
`BOOTSTRAP_ADMIN_*`; create vaults from the UI. The variables are re-applied at
every start, so they remain the Owner's Emergency Password: changing the
password revokes existing sessions. Removing them keeps the stored credentials.
The Owner has no username: `BOOTSTRAP_ADMIN_USERNAME` only marks the
credentials as configured, and the password alone signs in.

### Graceful shutdown

The image runs `python -m app.server`, a single-process uvicorn entrypoint that
signals SSE streams and stops background admission **before** uvicorn waits for
HTTP connections. SIGTERM (and SIGINT) closes active catalog streams normally;
idle and cooperatively draining uploads exit with status 0. Keep this entrypoint
when overriding the image command; plain `uvicorn app.main:app` has no early-stop
hook and can wait indefinitely for SSE.

HTTP graceful shutdown is bounded at 5 seconds (`--timeout-graceful-shutdown`);
worker pools then have up to 20 seconds to join running work. Queued pool work
is cancelled immediately, and active Jobs stop at existing transfer/verification
checkpoints. Shutdown is not an Owner cancellation: durable leases, upload
intents and late provider receipts remain available for conservative recovery
once the lease expires. No ambiguous send is blindly repeated.

Both repository Compose manifests set `stop_grace_period: 40s`. For a direct
container invocation use `docker stop --time 40` (Docker's 10-second default is
too short). A process-wide 30-second watchdog also covers synchronous SDK calls,
filesystem operations and Python executor joins that cannot be interrupted by
asyncio cancellation. If they do not drain in time, shutdown exits **nonzero**
rather than claiming success; recovery evidence is retained, but a provider
response arriving after that hard deadline cannot be recorded by the process.
Do not shorten the stop period below the process deadline.

## Recommended first smoke test

Before indexing a terabyte of data:

1. use a test source folder containing two unimportant files;
2. wait for the local catalog to pick them up through the filesystem watcher
   (or trigger an administrative `POST /api/vaults/{vault_id}/scan` if you need a full
   reconciliation immediately);
3. upload one file and confirm that a distinct S3 version exists in the bucket;
4. confirm the catalog keeps the version unverified until the requested SHA-256
   proof (or its visible exact read-back fallback) succeeds;
5. confirm **Free local space** and **Recover** remain unavailable for an
   unverified version;
6. keep the metadata backup master key and AWS access/recovery instructions in
   secure storage separate from the server.

The local catalog updates automatically when files under the configured source
are created, modified, renamed, or deleted. Native filesystem watching is the
primary mechanism; authenticated browsers receive a Vault-scoped catalog
revision signal and refresh the archive view without idle polling or a manual
**Refresh list** action. A low-frequency full scan (`SCAN_INTERVAL_SECONDS`,
default six hours) remains the reconciliation safety net for missed events and
mount-return correctness. Every healthy watcher start (new Vault, restart, or
recovery) also runs one full scan, so nothing that arrived earlier stays unseen. Set
`FILESYSTEM_WATCH_FORCE_POLLING=true` with Docker Desktop on Windows. On a Linux
server, prefer the native watcher with the value `false`.
See [docs/catalog-events.md](catalog-events.md) for the SSE journal cost
model and reconnect semantics.

## Indicative minimum AWS permissions

The application role/user must be able to list the bucket and, within the
configured prefix, read, upload, and request object restores. Automatic rename
cleanup requires `s3:DeleteObjectVersion` to remove the old key's versions
only after the new key is verified. The same permission removes superseded
versions after verified uploads; permanent purge uses it too.
The explicit seven-day delay applies to Owner-requested cloud deletion, not
verified snapshot replacement or automatic rename cleanup.

FrostVault creates and configures its own buckets, a Vault Bucket for every new
Vault and the System Bucket for metadata backups: attach
[`frostvault-buckets-policy.json`](../infra/terraform/archive-bucket/iam/frostvault-buckets-policy.json),
scoped to `arn:aws:s3:::frostvault-*`, which covers the operations above in those
buckets, grants `s3:DeleteBucket` only for an emptied Vault Bucket the Owner asked to delete (#739), never `s3:BypassGovernanceRetention` (see
[buckets FrostVault creates](aws-s3-bucket.md#buckets-frostvault-creates)).
Its one statement on `"*"` is `cloudwatch:GetMetricStatistics`: once a day
FrostVault reads the bytes per storage class and the object count of every
Vault Bucket from CloudWatch, shown on the Vault page next to the catalog's
estimate ([CloudWatch sizes](aws-s3-bucket.md#vault-bucket-sizes-from-cloudwatch)).
Without it the Vault page shows a note and only the estimate.

An installation from before Vault Buckets keeps its shared bucket for its older
Vaults (objects under `vaults/<uuid>/`); the Terraform baseline in
[`infra/terraform/archive-bucket/`](../infra/terraform/archive-bucket/) and
`iam/archive-operator-policy.json` describe that bucket. See [docs/aws-s3-bucket.md](aws-s3-bucket.md) for
the full model.

## Billed Cost from the AWS billing export

Each Vault shows its **Billed Cost**, what AWS actually charged for its Vault
Bucket, next to and apart from the Cost Estimate. It comes from an AWS Data
Exports CUR 2.0 export (gzip CSV, with resource IDs) that the Owner sets up once
in the AWS Billing console, delivered to the System Bucket under `billing/cur/`;
FrostVault reads it at most once a day and never calls the Cost Explorer API.
Without it only Cost Estimates are shown. Steps, bucket policy and IAM:
[docs/billed-cost.md](billed-cost.md).

## Container readiness

The image defines a healthcheck inherited by both Compose templates. Every 30s
it runs `python -m app.readiness_probe` against **`http://127.0.0.1:8080/ready`**
inside the container (3s socket timeout, 5s Docker timeout, 60s startup grace,
3 consecutive failures before `unhealthy`). `APP_PORT` changes only the host
mapping, not this internal port. No curl or additional probe package is needed.

With `ALLOWED_HOSTS` configured, the probe sends the first safe concrete hostname
or IPv4 entry as the `Host` header, preserving its exact spelling/case. Entries
are comma-separated and trimmed; empty entries are ignored. Use bare ASCII
hostnames (punycode for international names), **not** URLs, ports, credentials,
wildcards or shell/header fragments. Unsupported entries are skipped; a
non-empty allowlist with no safe entry fails closed. With an unset/empty list,
the probe sends `Host: 127.0.0.1`. The selected public hostname is **never resolved
or contacted**: the connection always stays on loopback and ignores HTTP proxy
environment variables.

Host protection is unchanged, including on loopback, `/health` and `/ready`.
Do not add loopback to `ALLOWED_HOSTS` or exempt health routes just to make a
check pass. Only HTTP 200 from `/ready` succeeds: database, worker heartbeat,
configuration or Source Volume layout failures return 503 and make the probe
fail. `/health` is process liveness only and is never a fallback. Redirects,
other statuses, invalid configuration, timeouts and connection failures fail too.

Read-only verification (also works behind Traefik, without a published port):

```bash
# Run exactly the packaged check; exit 0 = ready, exit 1 = failed.
docker compose exec -T frostvault python -m app.readiness_probe
# Inspect only health status/history, not the container environment.
docker inspect --format '{{json .State.Health}}' frostvault
```

For the Traefik template, add `-f compose.traefik.yaml` to the Compose command.
Probe failure output contains only a fixed diagnostic or HTTP status, not
configuration values or response bodies. During migrations/startup it can take
longer than the grace period to become ready; adjust `healthcheck.start_period`
in a local Compose override for a known long upgrade rather than weakening the
probe. Docker health status alone does not restart a container under
`restart: unless-stopped`; investigate the failed dependency before any restart.

## HTTP diagnostics

Diagnostics are **closed by default**, independently of Owner sign-in:

| Endpoint | Default / opt-in behavior |
| --- | --- |
| `/docs`, `/redoc`, `/openapi.json` (including Swagger's `/docs/oauth2-redirect`) | 404; `API_DOCS_ENABLED=true` enables anonymous HTTP docs/schema for isolated development only, not production |
| `/metrics` | 404 without `METRICS_TOKEN`; when set, only a valid `Authorization: Bearer` credential returns Prometheus text (missing, malformed or invalid credentials return 401) |
| `/health` | Anonymous minimal process liveness, `{"status":"ok"}` |
| `/ready` | Anonymous dependency status and boolean checks only; 200 ready / 503 not ready, no queue/workload details |

An Owner Session does **not** authorize metrics. Use a separate, strong random
scraper secret, not an Emergency Password, OIDC secret or Session credential.
Keep it outside version control and logs; never send it in a query string.
Responses containing metrics and credential errors use `Cache-Control: no-store`.
Host/origin/CSRF and trusted-proxy rules are unchanged; there is no loopback or
forwarded-header authentication exception. Disabled diagnostics never serve SPA
HTML. Local schema tooling remains usable without HTTP opt-in or any token:

```bash
.venv/bin/python scripts/export_openapi.py frontend/openapi.json
```

**Scraper migration:** previously anonymous scraping now fails closed. To retain
it, supply `METRICS_TOKEN` securely in the deployment environment and the same
value in the scraper's protected credential file, then apply the environment
change in an authorized restart/redeploy. Do not enable HTTP docs for scraping.
For example, through an existing HTTPS hostname (no new port or route):

```yaml
scrape_configs:
  - job_name: frostvault
    scheme: https
    authorization:
      type: Bearer
      credentials_file: /run/secrets/frostvault_metrics_token
    static_configs:
      - targets: ["frostvault.example.com:443"]
```

The credential file contains only the secret, not the `Bearer` prefix; it is
operator-provisioned, not supplied by this repository. Leave `METRICS_TOKEN`
empty if scraping is not needed. Prefer a private scrape path and deny diagnostic
paths at the public reverse proxy when not needed there; see
[the public-surface guidance](traefik.md#public-http-surface). The packaged
local healthcheck needs no credential or public diagnostic route and is unchanged.
