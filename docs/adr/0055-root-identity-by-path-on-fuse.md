# On FUSE the Root Identity is the Source Volume and the path

Status: accepted by the Owner on 2026-11-22 (issue #787). Amends ADR-0054.

## Context

ADR-0054 (`linux-stat-dir-v2`) kept the device number only for real block
devices and used the inode alone on anonymous devices, FUSE included. On a
union filesystem the inode is not stable either.

A union filesystem such as Unraid's user shares (`fuse.shfs`) shows one folder
as the union of the same path on several branches: a cache pool and the array
disks. shfs reports

```
st_ino = (branch_tag << 56) | (1 << 48) | <inode of the folder on the FIRST branch that has it>
```

so the inode of a folder changes whenever the folder starts or stops existing
on an earlier branch. On a share that uses a cache pool, a write creates the
folder on the cache (the inode becomes the cache one) and the nightly mover
moves the content to the array and removes the empty cache folder (the inode
becomes a disk one, or another disk's when the first disk no longer has the
folder). That happens without any reboot, here twice a week. A Vault rooted
there was blocked by every such move, the startup recovery of ADR-0054 could
not help (the inode changed too), and Reconnect Folder (a full re-read) was
the only way out, again and again. mergerfs and other FUSE unions behave the
same way.

## Decision

- **Root Identity `linux-stat-dir-v3`.** When the Source Volume holding the
  root is a FUSE filesystem (`fuse` or any `fuse.*` type, read from the
  mountinfo entry of that mount, never guessed from `st_dev`), the payload is
  the Source Volume Identity (the mount fingerprint of ADR-0035) and the
  root's path relative to that mount, with no inode and no device. The root
  must still be a real directory reached without any symlink, and its device
  number must be the one mountinfo lists for that mount (checked, never
  recorded). `fuseblk` is not included: it is a FUSE driver over a real block
  device, whose numbers are the disk's own. Every other filesystem keeps the
  v2 rule under the v3 version string: device and inode on a real block
  device, inode alone on another anonymous device (NFS, ZFS, btrfs, overlay).
- **An open descriptor must be the folder at the path.** A path identity
  cannot tell an unlinked or replaced open directory from the folder now at
  the path. Wherever a descriptor is verified (the pinned scan root, Excluded
  Folder browsing, the local absence proof of a cloud deletion), its device
  and inode must equal those of the symlink-free lookup of the Vault path,
  read again once on a difference (a union may report the new inode between
  the two reads), and only then is the identity of that path compared.
- **The in-scan root fence follows the identity.** A scan pins the root and,
  at its fences, compared the root's device and inode with the ones it had
  when the walk started; a mover run during a scan could change them and roll
  back the whole scan. On FUSE the root fence now asks whether the pinned
  descriptor is still the folder at the Vault path with the enrolled
  identity. Folders below the root keep the inode fence: one whose inode
  changes is a replaced subtree, compensated and walked again a bounded number
  of times (ADR-0043, #686).
- **Startup adoption** (`vault_relocation.upgrade_legacy_root_identities`,
  extended). For a Vault holding a v1 or v2 identity, active or waiting for a
  Reconnect, not released, whose Source Volume passes the scan gate (healthy,
  or waiting only for its full scan) and whose root path is a real directory:
  - on FUSE the identity is replaced by the path identity, without any inode
    proof, and the audit event `vault_root_identity_adopted` records
    `verified="source_volume_and_path"`, the previous and new versions and
    whether the old value still matched the folder. A healthy Vault already
    on v2 over FUSE moves too;
  - elsewhere v1 behaves as in ADR-0054 (silent rewrite when unchanged,
    verified recovery when only the anonymous device changed) and v2 is
    rewritten silently when it still matches;
  - otherwise the identity stays, and the scan reports `Vault root identity
    mismatch` (not "ambiguous": the identity exists, it names another
    folder). Reconnect Folder remains the recovery.
  It is idempotent, runs per Vault, never touches local observations and
  never starts a full re-read.
- **Every retired identity is kept** (migration `0019_retired_root_identities`):
  `vaults.retired_root_identities` replaces the single
  `legacy_root_identity`. Each upgrade appends the value it replaces, so a
  Vault upgraded v1 → v2 → v3 keeps both v1 and v2. Immutable proofs (upload
  snapshots and attempts, cleanup batches, expected proofs of landed versions,
  pending bulk uploads) pinned with any of them still name the current root
  (`app/services/root_pins.py`); a pending manual bulk upload keeps its
  admission. The migration moves the existing column value and invents
  nothing; enrollment, Reconnect Folder and a metadata restore empty the set.

## Alternatives rejected

- **Inode only (v2) on FUSE.** The inode is a union branch's, so it changes
  with the cache and the mover, as observed.
- **Marker file or extended attribute in the root.** As in ADR-0054: it needs
  write access to the user's folder, travels with copies of the folder (a
  backup copy would claim to be the Vault) and not every filesystem keeps
  xattrs.
- **A "root is not empty" or "catalogued files still present" guard.** It
  would block a legitimately emptied folder, read the folder before the
  identity is known, and protects only against the residual risk accepted
  below, which the cloud deletion caps already bound.
- **Accept a v1/v2 mismatch on FUSE only when the inode's low bits match a
  branch.** It depends on the private inode formula of one union filesystem
  and still fails when the folder moved to another branch.

## Consequences

- A Vault on a FUSE union keeps working across cache and mover activity, and
  across reboots, without Reconnect Folder. The first release with v3 adopts
  the Vaults already blocked by a mover run, with an audit trail.
- Accepted risk: on FUSE, a folder deleted and recreated at the same path of
  the same Source Volume keeps its identity. Files missing from it then look
  like local deletions: their cloud copies become cloud-only, and removing
  them from the cloud still goes through the 7-day Delayed Cloud Deletion or
  the Mirror Cleanup caps, so nothing is lost at once.
- A different relative path, or a different Source Volume Identity (another
  host folder bound at the same alias, another filesystem type or source),
  still changes the identity and blocks the Vault.
- Each identity check reads the mount metadata once, in addition to the
  Source Volume gate.
- A file's inode on a union also changes when the mover moves it; that is the
  file-level change detection's concern, not the Root Identity's.
