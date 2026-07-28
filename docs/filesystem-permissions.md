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

Override both when the host directories are owned by a different account. The
entrypoint creates a matching user/group when starting as root, then drops
privileges with `gosu`. Writable mounts are limited to `/sources`, `/data`, and
tmpfs `/tmp` + `/run` on a read-only root filesystem.

## Linux (native bind mounts)

```bash
export PUID=1000
export PGID=1000
sudo mkdir -p /srv/archive/sources /srv/archive/data
sudo chown -R 1000:1000 /srv/archive/sources /srv/archive/data
sudo chmod -R u+rwX,g+rX /srv/archive/sources
# Ensure new files inherit group write if multiple operators share the tree:
# sudo chmod -R g+w /srv/archive/sources && sudo chmod g+s /srv/archive/sources
```

Point `SOURCES_ROOT` at `/srv/archive/sources` and keep `./data` (or another
host path) owned by the same `PUID:PGID`.

## Unraid (SMB / nobody:users)

Unraid shares typically use `nobody:users` (`99:100`). Keep the defaults:

```bash
PUID=99
PGID=100
```

Map the array share into `/sources` and a cache or pool path into `/data`.
In the Unraid share settings, grant read/write to the user or group that maps
to `99:100`. Do not run the container as root to “fix” SMB permission errors;
fix the share ACLs instead. The panel reports unreadable files and unwritable
directories without rewriting modes.

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

If local scans report permission failures, align `PUID`/`PGID` with
`ls -ln` ownership as seen *inside* a temporary root shell, then recreate the
container. Avoid `chmod -R 777`; prefer owner/group write for the archive
identity only.

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

