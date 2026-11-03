# Verify Vault Root Relocation by enrolled directory identity

> Superseded by ADR-0019 (#427): one explicit Reconnect Folder accepts a
> different directory or Source Volume, retaining fail-closed identity checks
> and mandatory reconciliation. The original decision below is historical.

FrostVault permits a global administrator to reconnect a missing custom Vault
root only when the destination is on the same healthy, identity-verified Source
Volume and its opaque `linux-stat-dir-v1` identity (device plus directory inode)
matches the identity enrolled while the original root was healthy. This is a
Vault Root Relocation, not a generic rebind: content similarity, path names,
markers, and administrator intent never substitute for identity.

Relocation is rejected when the old root still exists, identity was not enrolled,
the filesystem cannot expose a stable inode, the destination differs, overlaps
another Vault, crosses a symlink or nested mount, is inaccessible, or any Job or
scan is active. The Vault-root allocation transaction lock serializes overlap
checks through a PostgreSQL transaction advisory lock (#422). The atomic update changes only `source_root` and
persistent relocation recovery state; Vault UUID, plaintext cloud namespace,
policies, Vault File and Archive Version rows, and audit identities
are untouched.

A successful update leaves local work and the filesystem watcher suspended in
`scan_required`. A mandatory full local scan clears that state; process restart
resumes the scan through the ordinary scheduled scan loop, and the watcher is
created only afterward. Failures before transaction commit roll back the path and
state. A failed post-commit scan leaves the verified destination suspended and
retryable rather than reverting to a known-missing path.

## Filesystem limitations

Linux directory inode identity is strong for a rename within one mounted local
filesystem, which is the supported operation. Some network, userspace, or
platform filesystems may recycle or change inode/device values. FrostVault does
not weaken the check for those filesystems: false negatives are possible and the
operator must restore the original layout; ambiguous identity fails closed.
Cross-volume moves, copied trees, automatic path guessing, and accepting a new
Source Volume identity remain unsupported.
