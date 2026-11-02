# FrostVault

This context tracks logical files in isolated vaults. Local Copies can appear,
change, or be removed. A Vault's Cloud History Policy is Archive History or
Current Snapshot.

## Language

**Vault**:
An isolated archive owned by the Owner as one policy and storage namespace.
_Avoid_: Bucket, folder, account

**Source Volume**:
An operator-provided local filesystem whose directory tree contains Vault roots;
the Owner may choose any directory under it as a new Vault root. FrostVault persists a markerless opaque identity for each
custom Source Volume and keeps local work suspended when its expected mount is
absent, inaccessible, ambiguous, unsupported, or replaced.
_Avoid_: Compose Volume, Storage Root, Mount

**Source Volume Identity**:
The versioned opaque fingerprint derived from one exact Linux mountinfo target;
it is stable across ordinary remounts and is never represented by a marker file
or by persisted raw host paths.
_Avoid_: Marker file, Host path, Mount ID

**Vault Root Relocation**:
Reconnecting a Vault to the same local directory after that directory moves
within its Source Volume.
_Avoid_: Rebind, Migration

**Vault Root Rebind**:
Retargeting a Vault to a different local directory rather than relocating the
same one.
_Avoid_: Relocation, Rename

**Vault Decommission**:
A deliberate, auditable lifecycle that quiesces one Vault, applies explicit
Local Copy and cloud-history dispositions, and releases its root only after both
are terminal and verified. Disabling a Vault is never a decommission.
_Avoid_: Disable, Delete Vault, Automatic release

**Vault Tombstone**:
The non-operational Vault and retained identity/history that remain after Vault
Decommission releases its root.
_Avoid_: Deleted Vault, Active Vault

**Vault File**:
A stable logical file in a Vault whose identity survives confirmed renames and
content changes.
_Avoid_: Archived File, object, path

**Path History**:
The ordered names and locations held by a Vault File over time.
_Avoid_: Alias list, current path

**Local Copy**:
The observed filesystem representation of a Vault File, including its last known
content fingerprint even when it is no longer present.
_Avoid_: Source file, local version

**Archive Version**:
One immutable, recoverable cloud representation of a Vault File's content.
_Avoid_: Object, backup, revision

**Cloud History Policy**:
The creation-time, immutable choice of how many Archive Versions a Vault keeps
recoverable.
_Avoid_: Versioning mode, Sync mode, Bucket versioning

**Archive History**:
A Cloud History Policy that keeps every verified Archive Version recoverable.
_Avoid_: Versioned, Full history, Versioning

**Current Snapshot**:
A Cloud History Policy that permits at most one recoverable Archive Version per
Vault File.
_Avoid_: Sync, Mirror, Unversioned, Revision

**Cloud-only Conflict**:
Different content observed at the path of a Vault File whose Local Copy was
absent, so its verified Archive Version was the only copy. The content is held
back from upload until it is renamed away or the owner confirms the replacement;
identical content is not a conflict.
_Avoid_: Overwrite, Collision, Duplicate

**Delete Marker**:
A reversible cloud marker that hides a key without containing or deleting an
Archive Version's data.
_Avoid_: Deleted version, empty version

**Integrity**:
The durable result of comparing an Archive Version's plaintext content with its
expected digest: unverified, verified, or mismatch.
_Avoid_: Upload status, availability

**Availability**:
Whether an exact Archive Version can currently be located in cloud storage:
unknown, available, missing, or purged.
_Avoid_: Integrity, restore state

**Job**:
A queued or running operation targeting one stable Vault File and, when known,
one exact Archive Version.
_Avoid_: Task, request

**Cost Estimate**:
An approximate storage or restore cost computed from the AWS list prices bundled
for the configured region. Prices are not editable and are never an AWS bill.
_Avoid_: Price book, Quote, Bill

**Upload Bandwidth Limit**:
The one optional, installation-wide cap on upload transfer speed. There are no
per-Vault quotas, limits, or operating windows; recoveries are never throttled.
_Avoid_: Quota, Operating window, Vault bandwidth

**Owner**:
The single person who operates a FrostVault deployment and owns every Vault.
There are no other users, roles, members, or shared Vaults.
_Avoid_: User, Administrator, Member, Account

**Identity**:
The external login bound to the Owner, identified by the issuer configured in
the environment and that provider's stable subject. The first successful OIDC
sign-in binds it; any other subject from that issuer is refused.
_Avoid_: Email, Login, Account

**Session**:
An expiring, revocable credential that represents the authenticated Owner on one
device.
_Avoid_: Cookie, Token, Login

**Emergency Password**:
The Owner's local password, set from the environment, accepted only from
loopback or the explicitly allowed LAN CIDRs. It is how the Owner signs in when
the OIDC provider is unavailable.
_Avoid_: Break-glass Login, Local Sign-in, Admin login

**Reauthentication**:
A fresh proof of the Owner's identity, required within a recent window before
downloading the Recovery Kit and before deleting from the cloud.
_Avoid_: Login, Re-login

**Trusted Proxy**:
A configured upstream whose forwarded client data (address, scheme, host) the
application is allowed to believe.
_Avoid_: Reverse proxy, Gateway
