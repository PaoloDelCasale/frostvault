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
- The file browser is home: a sidebar lists protected folders, All / Cloud only /
  Pending / Problems filters and **General**. Home reopens the last-used Vault at
  its root; search matches name/path across all protected folders and preserves
  Vault identity even for identical paths. Folder rows show protection counts.
- Selection offers only **Recover**, **Free space now** and single-file **Details**.
  Cloud internals and deliberate seven-day cloud deletion live only in Details.
  Cold recovery offers Normal (Standard) / Economical (Bulk, default), an estimate,
  return path and local-retention explanation. There are no previews.
- Each folder's gear panel contains automatic protection, fixed release periods,
  collapsed Advanced Storage Plan / Move now and Stop Protection. **General**
  contains optional email status, the installation Recovery Kit/fingerprint/custody,
  last verified backup, per-Vault storage estimates, folder creation and reconnect.
  There are no administration or Manage access pages. Installation settings remain
  deployment-owned. API clients should pin `vault_id` on every scoped request;
  an invalid explicit identity fails closed rather than using Session selection.

## Automatic protection

Browsing and statistics use five **protection states**, not copy-presence labels:

- **Pending**: new or changed local content not yet verified. A retained verified
  previous snapshot is indicated separately; it does not protect the new bytes.
- **Protected**: the regular Local Copy's fingerprint and matched Archive Version
  agree, with `integrity=verified` and `availability=available`.
- **Problem**: terminal upload failure, mismatched content verification, missing
  Archive Version, unsupported local entry, or a held Cloud-only Conflict.
- **Unconfirmed**: the Source Volume/reconnect gate prevents observation or cloud
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
- **Move the entire Vault now**, in the same Advanced panel, queues verified
  Current Snapshots for a colder class on the saved plan (not Standard). Mixed
  classes are evaluated per file: equal/colder copies, unverified/missing copies,
  pending work (including delayed deletion), and cold sources without an explicit
  exact-version restore are skipped. It never restores automatically or moves
  warmer. A regional storage/source-retrieval estimate and minimum-duration warning
  must be acknowledged first; request fees and early-deletion charges are excluded.
  A copy resets the provider object-age/plan clock. Exact destination checksum
  verification precedes removal of the exact old source; interruption resumes from
  a durable ledger without discarding the only verified copy. Follow/cancel Jobs
  in Operations; cancellation retains copies and does not undo finished movement.
  Reconnect and Stop Protection require these Jobs to finish or be cancelled.
  See [copy/restart safety and limitations](docs/adr/0021-vault-wide-colder-moves.md).
- Lifecycle synchronization preserves external bucket rules and the seven-day
  incomplete-multipart abort rule. Changing a plan never deletes cloud content.
- **Delete from cloud** is available only for **Cloud-only** files (or folders
  whose selected files are all Cloud-only). Type the exact Vault name and a
  reason, with recent Owner reauthentication. A fixed **seven-day cancellable
  delay** applies; there is no immediate deletion or acceleration. Every exact
  VersionId is captured once, never expanded to new content during execution.
  Local reappearance, changed cloud content or an untrusted/unavailable source
  cancels the request instead of deleting the only cloud copy by mistake.
- **Stop protecting this folder** always keeps local files. Choose **keep cloud**
  or the same delayed cloud deletion. A cloud-backed file present on the server
  blocks the delete choice: choose keep instead. Pending Jobs/scans must finish
  or be cancelled first. Stop+delete reserves the root until terminal; cancelling
  means keep cloud, not resume protection. Retained cloud content stays under its
  original S3 namespace and can be recovered using the Recovery Kit's manual S3
  instructions; the stopped Vault is a non-operational tombstone.
- Migration **0059** cancels legacy delayed deletion requests; make new explicit
  requests if still wanted. Finish legacy decommissions and reconcile interrupted
  destructive Jobs with the previous release before upgrading. No migration calls
  S3 or deletes local files. Remove `CLOUD_PURGE_DELAY_SECONDS`; it is no longer
  configurable. The old enablement toggle, generated phrase and decommission
  endpoints/UI are removed. A downgrade after Stop Protection is refused.
- Safe renames are recognized automatically only for unique, identical content
  observed within the scan window. The new cloud key is verified before cleaning
  up the old key (permanently under Current Snapshot). Ambiguous matches stay new
  files; no cloud content is deleted on their behalf. Path History is internal,
  not a UI action, and there is no manual rename confirmation.
- Archive statistics, file-state badges, and Job progress refresh automatically
  while background operations are active.

## Local cleanup safety

- Each Vault has one optional **Free local space after** rule, off by default:
  **3 months / 6 months / 1 year / 2 years** (fixed 90/180/365/730 days).
  Enabling preselects 6 months. It applies to every file, regardless of upload
  include/exclude filters. Put files that must stay local in a separate Vault
  with release off. Cold copies can take hours to recover and incur restore costs.
- Retention starts at the latest actual content observation, cloud content
  verification, local modification, or explicit recovery time. Old photo imports
  and recovered files get a full new period without changing their mtime.
- **Free local space now** remains an explicit, confirmed manual action even
  with the automatic rule off. Only Protected files are eligible; an absent or
  untrusted Source Volume, unconfirmed protection, mismatch, changed content or
  unavailable cloud blocks release. GLACIER and DEEP_ARCHIVE are allowed.
- Migration 0057 maps **all** old cleanup configurations to **off**, including
  enabled ones: the removed global environment gate cannot be inferred and old
  filters had a narrower scope. Pending legacy free-space Jobs are cancelled;
  request manual actions again, and explicitly enable the new rule if desired.
  Complete active local-removal decommissions before upgrading; cancellation of
  their pending Jobs leaves them blocked with the root reserved, never released.
  Upgrade refuses interrupted `cleaning` Jobs: reconcile them with the previous
  release first so hidden rename-claims are never stranded. No file/cloud content
  or verification evidence is changed. Downgrade also leaves automation off.
  Remove the retired `ALLOW_LOCAL_DELETE` environment variable; old API fields
  `auto_local_cleanup`, `local_retention_days` and `local_delete_enabled` are rejected.
  Policy updates preserve omitted fields, so saving upload rules cannot silently
  replay an old automatic-release choice from another settings panel.
- Release outcomes and blockages are audited and included in the weekly summary,
  never sent as per-file notifications.
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
- Sign in with the Owner's configured OIDC provider or, from loopback/allowed
  LAN, the Emergency Password.

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
warning. See [manual disaster recovery](docs/metadata-backups.md).

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
   catalog remains intact. Restore the expected Compose mount or explicitly
   **Reconnect Folder** for one existing Vault: accept a new volume/folder,
   reconcile S3 without deletions, then complete a full local rehash. Matching
   files become Protected, missing files Cloud-only and different content stays
   held as a conflict. Sibling Vaults are never implicitly accepted. Bind, Docker
   Desktop, and NAS details and limitations
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
   `TRUSTED_PROXIES` (the app's proxy IP/CIDR allowlist); the app refuses to start
   in production without them. **Also set `FORWARDED_ALLOW_IPS` explicitly** for
   Uvicorn to the same narrow proxy IPs/CIDRs as seen by FrostVault. Neither list
   derives from the other. Uvicorn rewrites both scheme and client address;
   `TRUSTED_PROXIES` cannot veto overly broad Uvicorn trust. Without the proxy in
   `FORWARDED_ALLOW_IPS`, OIDC redirects remain `http` even with secure cookies.
   Never use `*` or an entire Docker/LAN range as a shortcut; follow the
   [Traefik trust and verification guide](docs/traefik.md).
   Set `OIDC_ENABLED=true` and configure
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
SDK uploads use multipart transfers, a shared optional upload bandwidth cap and
throttled progress. New uploads default to **S3 SHA-256 ingestion proof**
(`UPLOAD_VERIFICATION=s3_checksum`); `read_back` requests an independent complete
GET/SHA-256. Vault settings offer inherit / S3 SHA-256 / full read-back and show
the deployment default and effective mode. The mode is frozen at Job admission;
later setting changes affect only new uploads, not retries or existing evidence.

Both plaintext SHA-256 and independently expected provider proof come from the
same stable Upload Snapshot. Single PUT uses SHA256 `FULL_OBJECT`; multipart uses
SHA256 `COMPOSITE` with independently hashed parts and recorded boundaries, **not**
the plaintext digest. Bounded validated part buffers prevent a changed Local Copy
from being sent under an earlier digest; no verification artifacts are written
inside the Vault or as an unbounded whole-file temporary copy. Normal buffers are
8 MiB, adjusted up to 1 GiB for large objects (5 TiB maximum), at most two per Job;
hashing reads 1 MiB chunks. Completed snapshot A never protects later Local Copy B.

The upload response supplies the exact VersionId and is registered before later
HEAD/proof checks. Valid S3 cryptographic proof grants Integrity verified and the
usual guarded Current Snapshot cleanup/local release. It proves **provider
ingestion**, not independently reread storage. Missing proof or an unsupported
checksum HEAD API triggers a visible full exact-VersionId GET/SHA-256 fallback;
wrong checksum/type/size/identity blocks, never silently downgrades. Read-back and
fallback download approximately the entire uploaded size (e.g. 9 GB upload +
9 GB download), potentially incurring request, retrieval and **S3 egress costs**.
File Details show the recorded method, fallback reason and completed read-back
byte count. Legacy versions without independent S3 expected evidence require
read-back against their durable plaintext digest, never remote checksum inference
or rehashing a changed Local Copy. Retries of linked versions do not issue another
PUT. Providers refusing checksum-bearing writes fail safely rather than silently
repeating an ambiguous upload without checksum.

Rename, Move now, Recovery and Reconnect retain their existing full/independent
verification behavior and exact VersionId pins; this is only a new-upload policy.
Migration `0063_upload_verification` adds nullable evidence and override fields,
without backfilling or rewriting existing trust. Downgrade discards the new method/
expected-provider fields, not plaintext digests or verified/available state.
See [ADR-0023](docs/adr/0023-new-upload-ingestion-proof.md) for the verification
contract and [ADR-0024](docs/adr/0024-upload-facts-and-proven-duplicate-cleanup.md)
for durable upload facts and replacement cleanup.
Incomplete multipart uploads are aborted on failure/cancellation, with a seven-day
lifecycle safety net for process interruption. No external transfer binary or
remote configuration is required.

### Interrupted upload and retained cloud extras

Migration `0064_upload_provenance` commits a target-specific upload intent,
independent Upload Snapshot and separate opaque metadata token before any cloud
mutation. Raw VersionId receipts append before proof/HEAD/progress flush or
cancellation, independently of a worker lease; a receipt is a fact, not permission
to publish or delete. PutObject/CreateMultipartUpload/CompleteMultipartUpload use
`total_max_attempts=1`; reads and same-MPU parts retain ordinary SDK retries.

Restart/retry reconciles the exact receipt or an exact unique token match against
that frozen expected proof before any new PUT. Zero matches after send-started,
multiple/fake tokens, incomplete/unstable pagination and observable external
writers remain **reconcile-only**, not evidence that another upload is safe.
Source A's receipt never verifies a later Local Copy B; an unavailable/replaced
Source Volume or changed Vault/path still suspends authority, even when individual
local-file absence would not prevent finishing an already-written target.

Upload and rename replacement cleanup share durable per-VersionId authority.
Scans discover content but do not directly delete it or grant trust by cataloging
it. Ordinary app-owned superseded versions require independent lineage. Historical
unknowns are adopted/cleaned only after a full exact-VersionId GET, checked length
and SHA256 prove a duplicate of a verified, available, current keeper. The proof
catalog does not promote an older duplicate to Current Snapshot or alter local
matches/verification clocks. A committed exact proof avoids repeated GET on retry.
Different, ambiguous, cold/unreadable versions stay in cloud with a deduplicated
Problem and a safe remedy. Historical reads may incur retrieval/egress/request
charges; explicitly restore an exact cold version externally if needed. No
arbitrary deletion or automatic RestoreObject action is offered.

File Details exposes **Retry exact reconciliation** independently of the normal
upload action (including already protected files for which upload returns 409).
`POST /api/upload/reconcile` admits one stable Vault File into a claim-fenced,
reconcile-only Job incapable of PUT; `/api/files/versions` exposes pending,
can_resume, bounded reason and remedy. Cancelled attempts retain facts and cannot
be automatically resurrected. New writers, path/owner/source changes, missing
keepers and Delete Markers block cleanup; only exact authorized IDs may be deleted,
never the keeper. Keep external S3/SMB writers quiescent: DB locks are not an atomic
provider snapshot, and in-flight deletes cannot be undone. Delayed deletion
consent, metadata-backup retention and generic prefix utilities are unchanged.

No automatic sweep or original-failure history resolution is added here; #459
owns that separate operational cadence. Verified is still not completed until
cleanup satisfies its postcondition. Downgrade refuses retained ambiguous sends
or pending cleanup authority before dropping metadata, without contacting S3.
No real AWS or object larger than 5 GiB acceptance is claimed by the compatible
CI emulator/SDK tests.

Migration `0051_plaintext_vaults` refuses legacy
encrypted Vaults before any schema change; recover test data using the previous
release before creating a fresh plaintext installation. It never rewrites or
deletes S3 objects. See [upgrade notes](docs/metadata-backups.md#older-formats-and-upgrades).

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
published on host ports. It requires both `TRUSTED_PROXIES` (app) and
`FORWARDED_ALLOW_IPS` (Uvicorn), explicitly configured to the actual trusted
proxy addresses, with no permissive defaults. See
[docs/traefik.md](docs/traefik.md) for topology selection and HTTPS OIDC checks.

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
The explicit seven-day delay applies to Owner-requested cloud deletion, not
verified snapshot replacement or automatic rename cleanup.

Use the Terraform baseline in
[`infra/terraform/archive-bucket/`](infra/terraform/archive-bucket/) or apply
equivalent versioning, encryption, public-access blocking, ownership, and
least-privilege controls. See [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md) for
the full model.

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

## Configuration

Start from `.env.local.example` for local PostgreSQL use or `.env.example` for a
PostgreSQL deployment. Every value containing `REPLACE` is a placeholder and
must be replaced before the corresponding integration is used. Important groups:

| Area | Variables |
| --- | --- |
| Database | `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` (or `.pgpass`); optional PostgreSQL `DATABASE_URL` overrides corresponding `PG*` values |
| Authentication | `BOOTSTRAP_ADMIN_*` (the Owner and its Emergency Password), `OIDC_*`, `BREAK_GLASS_ALLOWED_CIDRS` (LAN networks that accept the Emergency Password) |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES`, `FORWARDED_ALLOW_IPS` (Uvicorn) |
| Frontend | `FRONTEND_DIST_DIR` (Vite build output; required for HTML routes) |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `SMTP_TO_ADDRESS`, `SMTP_USE_TLS` (optional; see below) |
| Storage | `S3_BUCKET`, `VAULT_S3_BUCKET`, `AWS_DEFAULT_REGION` |
| Encryption and backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` |
| Operations | `OPERATION_CONCURRENCY`, `S3_DOWNLOAD_*`, `RESTORE_*` |

All installation configuration is environment-only and takes effect on process
restart. There is no mutable settings UI or API. Before upgrading through
migration `0048_essential_administration`, move any required database overrides
into deployment environment variables; the migration discards those overrides.
See [ADR-0009](docs/adr/0009-effective-system-configuration.md).

There is no audit or worker-error page. Sensitive/destructive actions (including
cloud deletion, Recovery Kit downloads and folder reconnects) retain an
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
- `APP_PUBLIC_URL` — optional canonical application URL (for example
  `https://frostvault.example.com`), without credentials/query/fragment. Enables
  email links to the file's folder, highlighted row and problem Details. Without
  it email still works but has no absolute application link.
- `SMTP_PORT` — defaults to `587`; valid range `1–65535`.
- `SMTP_USERNAME`, `SMTP_PASSWORD` — optional authentication. The password is
  never exposed by an API or stored in the database.
- `SMTP_USE_TLS` — defaults to `true`: STARTTLS on port 587 (and other ports),
  implicit TLS on port 465. Disable only for a trusted local relay.

Missing or partial configuration disables email, not the inbox. The General
page warns when email is not configured. The API exposes only a boolean
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
  there is no automatic conversion to PostgreSQL. See [metadata backups](docs/metadata-backups.md#older-formats-and-upgrades).
- The application protects archive workflows but does not replace offline,
  independently tested disaster-recovery copies.

## License

Licensed under the [Apache License 2.0](LICENSE).
