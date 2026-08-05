# Filesystem permissions

The archive container runs as a configurable non-root identity (`PUID`/`PGID`,
default `99:100` for Unraid compatibility). It never changes ownership or modes
under vault source directories. Operators must permission host paths explicitly
so upload, verified cleanup, and recovery can read and write as that user.

Symbolic links are rejected: they appear in the catalog as unsupported, are
blocked from upload and cleanup, and are listed in vault filesystem health.

## Container identity

| Variable | Default | Meaning |
|----------|---------|---------|
| `PUID` | `99` | Numeric user id inside the container |
| `PGID` | `100` | Numeric group id inside the container |

The image bakes in the one documented default account, `archive` (`99:100`).
It **never creates users or groups at runtime**. Both supplied Compose manifests
set `user: "${PUID:-99}:${PGID:-100}"`, so Docker starts the entrypoint directly
as that numeric identity; an override does not need a matching `/etc/passwd` or
`/etc/group` entry. This is required because the manifests keep
`read_only: true`, `cap_drop: [ALL]`, and `no-new-privileges`.

A direct `docker run` that does not set `--user` begins as root because the
image does not declare `USER`; the entrypoint can use `gosu` to drop to a
valid numeric override when the normal set-ID capabilities are available. That
convenience path is not used by Compose and must not be used to work around
bind-mount permissions.
Writable mounts are limited to `/sources`, `/data`, and tmpfs `/tmp` + `/run` on
a read-only root filesystem.

### Compose `./data` preflight

Before the **first** `docker compose up` (or `docker compose -f
compose.traefik.yaml up`), create the repository's `./data` bind source on the
host and give that directory to exactly the identity selected in `.env`. Docker
would otherwise create a missing bind source as root, and the non-root Compose
process could not create the SQLite database, migration files, or backup
directory.

For the default Unraid identity (omit `sudo` when your shell is already root,
as is typical on Unraid):

```bash
mkdir -p ./data
sudo chown 99:100 ./data
sudo chmod 0750 ./data
```

For an override, use the same numbers in `.env` and in the host command (do not
source `.env` as shell code):

```bash
PUID=1000
PGID=1000
mkdir -p ./data
sudo chown "${PUID}:${PGID}" ./data
sudo chmod 0750 ./data
```

These commands intentionally affect only the fresh `./data` directory; they
are not a recursive repair recipe for an existing catalog. They do **not**
change `SOURCES_ROOT` or any content under `/sources`. Prepare Source Volumes
according to their own access policy before deployment, then preserve their
ownership and modes while FrostVault runs.

### Valid identity values and Compose errors

Set both values to canonical, non-root decimal IDs from `1` through
`2147483647`, with no sign, whitespace, or leading zero. `0` is rejected by the
entrypoint even though Docker can start a root process. Values above
`2147483647` are rejected by current Docker daemons while processing Compose's
`user` field. An unknown non-numeric `PUID` or `PGID` is likewise rejected by
Docker during its user/group lookup before `/entrypoint.sh` runs; a non-numeric
value that happens to name an existing account reaches the entrypoint and is
then rejected there. Therefore a malformed Compose identity does not always
produce the entrypoint's error message. Compose expands the value but does not
validate it: `docker compose config` can render a malformed `user` successfully,
then the Docker daemon rejects it when the service starts. Fix `.env` and use
`docker compose config` to inspect the rendered value; do not add capabilities
or run as root to bypass the rejection.

## Linux (native bind mounts)

```bash
export PUID=1000
export PGID=1000
sudo mkdir -p /srv/archive/sources
# Apply only to a Source Volume whose access policy intentionally uses 1000:1000.
sudo chown -R 1000:1000 /srv/archive/sources
sudo chmod -R u+rwX,g+rX /srv/archive/sources
# Ensure new files inherit group write if multiple operators share the tree:
# sudo chmod -R g+w /srv/archive/sources && sudo chmod g+s /srv/archive/sources
```

Point `SOURCES_ROOT` at `/srv/archive/sources`. Separately run the fresh
`./data` preflight above before Compose starts; do not rely on the container to
create or repair that directory.

## Unraid (SMB / nobody:users)

Unraid shares typically use `nobody:users` (`99:100`). Keep the defaults:

```bash
PUID=99
PGID=100
```

Map the array share into `/sources` and a cache or pool path into `/data`.
Before Compose starts, create/own/mode the repository's `./data` directory with
the `99:100` preflight above (or use an equivalently prepared cache/pool bind
source). In the Unraid share settings, grant read/write to the user or group
that maps to `99:100`. Do not run the container as root to “fix” SMB permission
errors; fix the share ACLs instead. The panel reports unreadable files and
unwritable directories without rewriting modes.

## Docker Desktop (macOS / Windows)

Docker Desktop remaps bind-mount UIDs. Prefer keeping `PUID`/`PGID` at the
defaults and ensuring the mounted folders are writable from the container:

```bash
mkdir -p ./local-data/sources ./data
# Desktop usually presents mounts as writable to the container user.
PUID=99
PGID=100
SOURCES_ROOT=./local-data/sources
```

The Desktop file-sharing layer may not expose host `chown` semantics, but
`./data` must still exist before `docker compose up` and be writable as
`99:100` from inside the container. Verify that with a temporary container or
the first start; fix the shared-folder permission in Docker Desktop rather than
adding root/capabilities. If local scans report permission failures, align
`PUID`/`PGID` with `ls -ln` ownership as seen *inside* a temporary root shell,
then recreate the container. Avoid `chmod -R 777`; prefer owner/group write for
the archive identity only.

## Diagnostics

`/api/stats` includes a `filesystem` object with effective `uid`/`gid`, root
read/write/execute checks, and per-path findings (`fs.unreadable_file`,
`fs.unwritable_directory`, `fs.symlink`). The vault page surfaces the same
health summary. Findings never trigger automatic `chown` or `chmod`.

## Fixed Source Volume layout

FrostVault expects a flat container layout under `/sources`:

- `/sources` itself must be a real writable mount;
- `/sources/managed` is reserved and created at startup (never chown/chmod it);
- every other direct child must be a real `rw` mount (`/sources/<alias>`);
- nested mounts inside a Source Volume are unsupported because one Vault must
  never cross filesystem boundaries.

Host Compose still uses `SOURCES_ROOT` only as the bind source for `/sources`.

## Markerless Source Volume identity

For each custom `/sources/<alias>` FrostVault parses the exact mount-target row
from Linux `/proc/self/mountinfo`. It hashes a versioned canonical tuple of the
stable filesystem type, mount source, and bind root. Mount IDs, device numbers,
propagation fields, and mount/superblock options are excluded because they are
transient across normal restart/remount. Raw mount source and bind-root values
are never stored or logged; only the opaque fingerprint is persisted. No marker
file is written into user data.

The expected fingerprint is immutable. A normal remount with the same stable
fields is accepted. A different fingerprint is **replaced** and fails closed
before scans, watchers, browsing, upload, recovery, rename, free-space, or other
local work can touch the tree. Retry and rescan cannot accept a replacement;
restore the expected operator/Compose mount. Catalog and cloud-only views remain
available, and a restored expected mount requires a full local scan. Moving a
Vault root within that same healthy Source Volume remains a separate explicit
relocation concern; this mechanism never rebinds a Vault or Source Volume.

Supported deployment notes:

- **Linux bind mounts:** supported when the kernel exposes one exact mountinfo
  row with a non-empty filesystem type, source, and bind root. Remount IDs and
  option changes do not alter identity. Changing the host bind root is treated
  as replacement even if an operator believes its contents are equivalent.
- **NAS/network mounts:** NFS/NFS4, CIFS/SMB and other real filesystems are
  supported when mountinfo exposes stable source/export and root fields. A
  server/export change is replacement. Operator-side aliases that make two
  different exports expose identical metadata cannot be distinguished.
- **Docker Desktop:** virtiofs/osxfs or VM-backed bind mounts are supported only
  when Desktop exposes stable, sufficient source/root metadata to the Linux
  container. Desktop upgrades or file-sharing changes that rewrite those stable
  fields intentionally fail closed; restore the previous mapping. Platforms
  that hide the metadata are unsupported rather than silently trusted.
- **Unsupported/ambiguous cases:** pseudo filesystems (including overlay and
  tmpfs), missing/placeholder fields, inaccessible mountinfo, no exact target,
  and stacked duplicate rows for one exact target are blocked. Remove duplicate
  mounts or provide one direct supported mount. Nested mounts remain
  unsupported regardless of fingerprint strength.

The administration Source Volume inventory retains persisted aliases while a
mount is absent and distinguishes **absent**, **inaccessible**, and **replaced**
health. FrostVault does not expose an “accept replacement” endpoint or UI.

