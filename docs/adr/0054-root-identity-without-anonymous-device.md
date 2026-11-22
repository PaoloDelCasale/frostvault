# The Root Identity ignores anonymous device numbers

Status: accepted by the Owner on 2026-11-22 (issue #784).

Amended by ADR-0055: on a FUSE Source Volume the Root Identity is the Source
Volume Identity and the root's path (`linux-stat-dir-v3`), and every retired
identity is kept, not only the v1 one.

## Context

A Vault's **Root Identity** (`vaults.root_identity`, version
`linux-stat-dir-v1`) was the sha256 of the root directory's `st_dev` and
`st_ino`. Every scan, upload, exclusion and cloud deletion compares the folder
on disk against it and fails closed when it differs.

On Unraid, the user shares are `fuse.shfs`. FUSE, like NFS, ZFS, btrfs subvolumes
and overlayfs, gets an *anonymous* device number (major 0) when it is mounted,
in mount order. After an ordinary host reboot (Unraid update, 2026-11-21) it
became `0:46` instead of `0:47`. Every Vault on a user share was then blocked
with `Vault root identity mismatch; local scan blocked`: `st_ino` was the same,
and so were the compose file and the bind mounts. The only recovery,
Reconnect Folder, re-reads every file (about 1.9 TB / 262k files here) and
would have been needed again after every reboot.

The Source Volume Identity (mountinfo fingerprint, ADR-0035) already leaves
device numbers out, *because they can change during an ordinary remount*. The
Root Identity contradicted it.

## Decision

- **Root Identity `linux-stat-dir-v2`.** For a real block device
  (`os.major(st_dev) != 0`) the payload keeps `dev` and `ino`. For an
  anonymous device (major 0) it keeps `ino` alone and records `"dev": null`,
  so the two shapes can never collide. Whether the filesystem is still the
  same one stays the Source Volume Identity's job, and every local operation
  already requires that gate. The identity stays markerless: FrostVault writes
  nothing in the folder.
- **Startup upgrade of v1 identities**
  (`vault_relocation.upgrade_legacy_root_identities`, right after the
  enrollment of legacy roots and before Jobs, workers or watchers). It covers
  Vaults that are active or waiting for a Reconnect, whose root is not
  released, and whose Source Volume passes the scan gate (healthy, or waiting
  only for its full scan):
  - the stored v1 hash equals the v1 hash of the current root: rewritten as v2
    silently;
  - otherwise, on an anonymous device, the hypothesis "only `st_dev` changed"
    is verified. The exact v1 payload bytes are rebuilt with the current
    `st_ino` and every anonymous device number (`makedev(0, minor)`, all
    2^20 minors; about 0.5 s). If exactly one reproduces the stored hash, the
    identity is rewritten as v2 and the audit event
    `vault_root_identity_recovered` records the old and new device. This runs
    at most once per Vault per process;
  - no match, a real block device whose number changed, or a symlink or a file
    in place of the folder: the identity is left as it is. The Vault stays
    blocked and Reconnect Folder remains the recovery.
  A Vault skipped because its Source Volume was down is retried by the
  background loop once the volume is back. The upgrade never touches local
  observations, never starts the Reconnect full re-read and invents nothing.
- **The retired v1 value is kept** in `vaults.legacy_root_identity` (migration
  `0018_legacy_root_identity`, NULL for every existing Vault). Upload
  snapshots, cleanup batches and the expected proofs of landed versions pin
  the Root Identity they were written with and are immutable. Upload
  admission, Upload Remediation and snapshot cleanup treat a pin holding the
  retired value as naming the current root (`app/services/root_pins.py`). Every
  other difference is still a changed root. A pending manual bulk upload of the
  Vault keeps its admission instead of being invalidated. Reconnect Folder,
  first enrollment and a metadata restore clear the column.

## Alternatives rejected

- **Drop `st_dev` always.** Two block filesystems share root inode numbers
  (ext4's root is always inode 2). A Vault rooted at the top of a disk would
  keep its identity when another disk is mounted at the same place, a
  replacement the Source Volume fingerprint does not always tell apart
  (same source and type after a disk swap).
- **Marker file or extended attribute in the root** (a Vault UUID). It
  contradicts the markerless identity (ADR-0035): it needs write access to the
  user's folder, it is copied along with the folder (a backup copy would claim
  to be the Vault), and not every filesystem keeps xattrs.
- **Accept any v1 mismatch whose inode matches.** Without rebuilding the exact
  stored bytes nothing proves that only the device number changed; the
  verified search costs half a second per Vault once.
- **Re-pin in-flight proofs to the v2 value.** Upload snapshots and attempts
  are immutable provenance guarded by triggers; rewriting them would erase
  what they were admitted for.

## Consequences

- An ordinary reboot, array stop/start or remount of a user share no longer
  blocks its Vaults, and the first release with v2 unblocks the Vaults already
  blocked by a reboot without a full re-read, with an audit trail.
- On an anonymous-device filesystem, a folder deleted and recreated with the
  same inode number would keep its identity. Inode reuse within the same
  filesystem at the same path is the accepted residual risk; the Source Volume
  Identity still catches a different filesystem.
- A real block device whose number changes (another disk order at boot) still
  blocks its Vaults, as before.
- Upload blocks already recorded as `identity_changed` while a Vault was
  blocked are not cleared by the upgrade; "Retry failed uploads" re-admits them.
