# Filesystem permissions

The archive container runs as a configurable identity (`PUID`/`PGID`, default
non-root `99:100` for Unraid compatibility; root as an explicit option, see
[Running as root](#running-as-root-puid0)). It never changes ownership or modes
under vault source directories, except that a Recovery gives the recovered file
its [original owner and mode](#original-owner-and-mode-in-recovery). Operators must grant the runtime identity read
and traverse access for cataloging, upload, and streamed upload verification.
Verification reads the remote plaintext without creating a temporary file in the
Vault root. Recovery, rename, and Local Copy cleanup remain write operations and
also require write access to the applicable directory.

Symbolic links are rejected: they appear in the catalog as unsupported, are
blocked from upload and cleanup. Scans never follow them.

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
process could not create migration scratch files or encrypted backup
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

Set both values to canonical decimal IDs from `1` through `2147483647`, with no
sign, whitespace, or leading zero, or both to `0` for the
[root mode](#running-as-root-puid0). A single `0` (for example `PUID=0
PGID=100`) is rejected by the entrypoint. Values above
`2147483647` are rejected by current Docker daemons while processing Compose's
`user` field. An unknown non-numeric `PUID` or `PGID` is likewise rejected by
Docker during its user/group lookup before `/entrypoint.sh` runs; a non-numeric
value that happens to name an existing account reaches the entrypoint and is
then rejected there. Therefore a malformed Compose identity does not always
produce the entrypoint's error message. Compose expands the value but does not
validate it: `docker compose config` can render a malformed `user` successfully,
then the Docker daemon rejects it when the service starts. Fix `.env` and use
`docker compose config` to inspect the rendered value; do not add capabilities
to bypass the rejection.

## Two modes: 99:100 (default) or root

| | `PUID=99 PGID=100` (default) | `PUID=0 PGID=0` |
|---|---|---|
| Who it is | `nobody:users`, the usual owner of SMB shares | root **without** root privileges |
| Upload and scan | files readable by `99:100` (others' `644` included) | the same, plus files readable only by root |
| Recovery in place | only in directories writable by `99:100`; the file belongs to `99:100` | only in directories writable by root: root's `755` ones, not `99:100 0755`; the file gets its original owner and mode |
| Suited to | shares written over SMB or by containers running as `99:100` | shares written by containers running as root (Immich, Frigate) |

Upload and scan work in both modes on the typical Unraid shares. Recovery in
place does not work with `99:100` on shares that containers running as root
wrote (`root:root`, directories `755`): no directory there is writable.

Root without the capabilities that bypass permissions is subject to the same
checks as any user: it **cannot create, rename or remove entries in folders of
other owners** (for example `99:100 0755`). Recovery in place, freeing local
space and Reconnect Folder fail there. So use root only for trees owned by root;
for a share of one other owner set `PUID`/`PGID` to that owner. For shares with
several owners see [Shares with several owners](#shares-with-several-owners-frostvault_allow_dac_override1).
FrostVault checks the folder before it writes (a Recovery before it asks S3 for
a restore) and says which folder, its owner and mode, and FrostVault's identity:
«FrostVault's user (0:0, groups 100) may not write the folder …: it belongs to
99:100 with mode 0755».

### Running as root (PUID=0)

Root is an explicit choice: set **both** `PUID=0` and `PGID=0`, and harden the
container so uid 0 is only an ordinary owner of root's files:

```yaml
    environment:
      PUID: 0
      PGID: 0
    user: "0:0"
    # Root reads the 0640 files of the users group (Unraid's PGID 100).
    group_add: ["100"]
    cap_drop: [ALL]
    cap_add: [CHOWN, KILL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - "/tmp:uid=0,gid=0,mode=1777"
      - "/run:uid=0,gid=0,mode=0755"
```

With the supplied manifests, `PUID=0` and `PGID=0` in `.env` already render
`user: "0:0"` and the tmpfs owners; add `group_add` and
`cap_add: [CHOWN, KILL]` in an override. `user: "0:0"` drops the supplementary
groups: without `group_add` (use your share's group, often `100` on Unraid)
root cannot read files that other owners made readable only to their group,
and the startup log warns about it.

Before anything starts as root, the entrypoint reads `CapEff` and `CapBnd` from
`/proc/self/status` and **refuses to start** (exit code 64, with the reason) if
the process holds, or could regain, any capability that bypasses file
permissions:

- `CAP_DAC_OVERRIDE` and `CAP_DAC_READ_SEARCH` (read, write and traverse
  anything), unless explicitly allowed for
  [shares with several owners](#shares-with-several-owners-frostvault_allow_dac_override1);
- `CAP_FOWNER` (change modes and timestamps of others' files);
- `CAP_SYS_ADMIN`;

or if `no-new-privileges` is not active (`NoNewPrivs: 1`). A process with uid 0
and none of these capabilities is subject to the normal permission checks: it
gets access to files owned by root (Immich, Frigate), not to everything. There
is no switch to another user with `gosu`. The container logs and the **System**
page say «FrostVault runs as root».

Capabilities added back in root mode:

| Capability | Why |
|---|---|
| `CHOWN` | gives a recovered file its original owner; without it the file stays `0:0` and the Job outcome says so |
| `KILL` | optional: FrostVault signals only processes of its own uid, which needs no capability; it bypasses no file permission |

### Shares with several owners (FROSTVAULT_ALLOW_DAC_OVERRIDE=1)

When one share holds folders of several owners (for example `root` from Immich
and `99:100` from SMB) neither identity can recover everywhere. Only then, set
`FROSTVAULT_ALLOW_DAC_OVERRIDE=1` with `PUID=0 PGID=0` and add the two
capabilities back:

```yaml
    environment:
      PUID: 0
      PGID: 0
      FROSTVAULT_ALLOW_DAC_OVERRIDE: "1"
    user: "0:0"
    cap_drop: [ALL]
    cap_add: [CHOWN, KILL, DAC_OVERRIDE, DAC_READ_SEARCH]
    security_opt: ["no-new-privileges:true"]
```

The one variable allows both `CAP_DAC_OVERRIDE` and `CAP_DAC_READ_SEARCH`; the
entrypoint refuses them without it, and refuses the variable with any other
`PUID`/`PGID`. `CAP_FOWNER`, `CAP_SYS_ADMIN` and a missing `no-new-privileges`
stay refused. The startup log and the **System** page say that FrostVault
bypasses file permissions.

Risks: FrostVault then reads, writes and deletes **every** file mounted into
the container, whatever its owner and mode, like a full root process on the
host for those mounts. A compromised FrostVault, or a bug in a write path, is
no longer limited by any permission under `/sources`. Mount only the shares
FrostVault protects, read-only where it never recovers or frees space, and
prefer a `PUID` per share when the owners are separate trees.

Risks: a compromised FrostVault running as root can modify every file owned by
root that is mounted into the container, in addition to everything writable by
any user (`666`/`777`, typical on Unraid shares, which `99:100` can already
modify). It cannot read files of other owners without read permission, cannot
change their permissions, and cannot gain capabilities. Mount into `/sources`
only the shares FrostVault protects. The gain in security of `99:100` is small
on `nobody:users 777/666` shares, while its cost is a Recovery that does not
work on the data that containers running as root wrote. Most containers on
Unraid, Immich and Frigate included, make the same choice.

## Original owner and mode in Recovery

Each upload records the uid, gid and permission bits (`chmod` mode, including
set-user-ID, set-group-ID and sticky) of the Local Copy it hashed, next to its
modification time: in the catalog (Upload Snapshot) and in the S3 object's
user metadata `frostvault-uid`, `frostvault-gid` and `frostvault-mode` (4-digit
octal). A metadata backup carries the catalog copy; a catalogless Recovery reads
the S3 copy of the exact version.

A Recovery first publishes the verified file, then, through the open file of
the published inode, sets the original mode and then the original owner:

- with `CHOWN` (root mode) the file comes back as it was, for example
  `root:root 644` or the SMB user's `1002:100 766`;
- when the owner may not be changed (any other `PUID`, or root without
  `CHOWN`) nothing changes from before: the file keeps the runtime identity's
  owner and default mode, and the Job outcome says that the original owner and
  mode were not applied;
- if the file already belongs to the runtime identity, its mode is applied.

The kernel clears the set-user-ID and set-group-ID bits when the owner changes;
without `CAP_FSETID` FrostVault cannot set them again on a file of another
owner. Files uploaded before v0.8.0 have no recorded owner: with `CHOWN` they
get the owner (uid and gid) of the folder they are recovered into, so a share of
`99:100` folders does not fill with `0:0` files; their mode is the default one.
Without `CHOWN` they keep the runtime identity, and the Recovery does not fail.
Directory owners and modes created by a Recovery are not restored.

A Recovery that fails after its S3 restore completed (for example because the
folder became unwritable) says until when the restored copy stays at AWS:
fix the cause and press Recover again, which reuses it without a new restore.

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
errors; fix the share ACLs instead. Use the [root mode](#running-as-root-puid0)
for shares that containers running as root write (for example the Immich
library or Frigate recordings), where Recovery in place needs it. The panel reports unreadable files and
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

## Filesystem problems

The actual scan and watcher check file readability. Each unreadable file or
subdirectory creates a deduplicated problem with its relative path in the
notification center, without a sample limit. An incomplete scan never marks
unseen Local Copies missing. An inaccessible or unverifiable Vault root blocks
protection. FrostVault never changes ownership or permissions automatically.

An unreadable subdirectory is reported by name, including a directory briefly
inaccessible during native watcher registration. Native registration does not
ignore permission errors: a successful timeout must not conceal a directory it
failed to enroll. The named gap is retained even if permissions are repaired
before the next batch or scan, and the watcher restarts with the readable tree
registered around it (using the existing bounded restart backoff). No chmod,
chown, later directory creation, or application restart is needed to trigger
recovery: the same once-a-minute gap recheck restores coverage and a paired
catch-up scan reconciles files written before registration. A manual scan can
repair the desired plan, but cannot erase this registration debt.

An unreadable directory is not watched, but the watcher of the rest of the
Vault resumes, whichever folders sit next to it:
the readable part of the tree is registered around the gap, and registered as a
whole again once the folder is repaired; the log says so in one line, without a
traceback. The Vault stays `unconfirmed` while a path
is unreadable: the other files are still uploaded, nothing is deleted from the
cloud, and no local space is freed on that path, below it or in the folders
above it. Local Copies elsewhere in the Vault can still be released
([ADR-0042](adr/0042-local-release-blocked-by-path.md)); the refusal names the
path. When it can be stat'ed, the problem shows the path's owner and mode and
FrostVault's identity («owned by uid 99 with mode 0700, not readable by
FrostVault's user 0:0, groups 100»). Because fixing permissions raises no
event, FrostVault looks at the reported paths once a minute and scans as soon as
one is readable; the problem then closes by itself, path by path. (With
`FILESYSTEM_WATCH_FORCE_POLLING=true` the same plan is used, so the poller never
lists a folder it may not read; a folder that becomes unreadable during the
watch stops the poller once, and the restart registers around it.)

A successful walk alone does not confirm a repaired subtree: the watcher first
passes the registration barrier with the expanded coverage, then runs a paired
catch-up scan before `source` health clears. This applies to both native and
polling observers. Files that arrived before registration are caught by that
scan; files created at or after confirmation are already watched. The catch-up
scan also runs when only some gaps are repaired; unresolved gaps still keep the
Vault unconfirmed. Merely discovering a new gap does not add another scan.
A discovered directory also records coverage debt independently of the latest
scan's desired exclusions. A concurrent manual scan cannot erase this debt by
repairing the desired plan before the watcher consumes it: even identical empty
plans require a fresh registration and catch-up. Each registration settles only
the debt captured before its construction, never a newer discovery made while
it was registering. A scan captures both missing coverage and its registration
generation before walking. A scan already in flight before a recovery barrier
cannot confirm the recovered coverage, even if the debt was discovered after
its walk started; a subsequent complete walk can.

A file or folder name that is not valid UTF-8 (typical of old archives or SMB/NFS
shares with another encoding) cannot be catalogued or become a cloud key. It is
reported as an unsupported name with the path shown in escaped form
(`non-utf8-\udce9.txt` stands for the byte `0xE9`) and skipped together with
everything beneath it, which is not watched in real time either. Because such a
name may be the new name of something that was catalogued under a different
text, the Vault stays `unconfirmed` while it exists and no file is marked missing:
the other files are still uploaded, nothing is deleted from the cloud, and no
local space is freed on that path, below it or in the folders above it. A file the watcher sees disappear next to such a name is
not marked missing either: it may only have been renamed. Rename it (from a
client that shows the real name); the problem closes at the first scan that no
longer finds it.

Every healthy watcher start (a new Vault, a restart, or a return after a failure
or after the Vault folder came back) is followed by a full scan, so files that
appeared while nothing was watching are never invisible until the six-hour scan.
Those scans run one Vault at a time, and a watcher that keeps stopping causes at
most one scan every five minutes. When S3 becomes reachable again after a blocked
readiness check, the Vaults whose last cloud listing failed are scanned.
See [ADR-0027](adr/0027-watcher-start-is-paired-with-a-scan.md).

There is no dedicated diagnostics banner or background tree inspection.
`/api/stats` returns catalog aggregates and a minimal `source_volume` safety
state; it never resolves or walks an identity-unsafe root. The Source Volume
inventory API remains available for mount identity and access information.

## Fixed Source Volume layout

FrostVault expects a flat container layout under `/sources`:

- `/sources` itself must be a real writable mount;
- `/sources/managed` is reserved and created at startup (never chown/chmod it);
- every other direct child must be a real `rw` mount (`/sources/<alias>`);
- nested mounts inside a Source Volume are unsupported because one Vault must
  never cross filesystem boundaries.

A path counts as a mount when `/proc/self/mountinfo` lists a mount at exactly
that path, so a bind mount of a folder on the same filesystem as `/sources`
(common on Unraid, e.g. `<pool>/photos:/sources/photos` next to
`<pool>/frostvault-sources:/sources`) is a valid Source Volume.

A missing or non-mounted `/sources`, or an invalid `/sources/managed`, makes
the whole instance not ready. A direct child that is not a mount (an ordinary
directory, a file or a symbolic link) affects only that alias: it appears in
the Source Volume inventory with health `invalid`, cannot hold Vault roots,
and its Vaults' local work stays suspended, while `/ready` and every other
Source Volume keep working.

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
restore the expected operator/Compose mount or explicitly use **Reconnect
Folder** for the selected Vault. Catalog and cloud-only views remain available.
Reconnect accepts the observed supported mount fingerprint for that Vault only;
it never replaces shared enrollment or unblocks sibling Vaults. It accepts a
different directory/Source Volume with root containment/allocation checks, then
reconciles cloud without deletions and requires a complete local rehash before
work resumes. Different bytes are held as Cloud-only Conflicts. Future mount or
directory replacement fails closed again (ADR-0019).

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

The General Source Volume inventory retains persisted aliases while a
mount is absent and distinguishes **absent**, **inaccessible**, and **replaced**
health. **Reconnect Folder** is the sole explicit per-Vault acceptance action
(`POST /api/vaults/{vault_id}/reconnect`, Session + CSRF + reason). Shared
inventory may still say replaced while a specifically accepted Vault is healthy;
other Vaults stay blocked. Missing/ambiguous/unsupported mounts are never accepted.
The superseded `/relocate` endpoint no longer exists.

