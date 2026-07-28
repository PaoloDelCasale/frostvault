# FrostVault

This context tracks logical files in isolated vaults and preserves recoverable
cloud history while local copies can appear, change, or be removed.

## Language

**Vault**:
An isolated archive owned and shared as one policy and storage namespace.
_Avoid_: Bucket, folder, account

**Source Volume**:
An operator-provided local filesystem whose directory tree may contain Source
Areas and Vault roots.
_Avoid_: Compose Volume, Storage Root, Mount

**Source Area**:
A reusable, non-overlapping local directory tree assigned exclusively to one
User, including its root and descendants, from which that User may choose roots
for new Vaults.
_Avoid_: Visible Path, Volume Grant, Path Permission

**Vault Root Relocation**:
Reconnecting a Vault to the same local directory after that directory moves
within its Source Volume.
_Avoid_: Rebind, Migration

**Vault Root Rebind**:
Retargeting a Vault to a different local directory rather than relocating the
same one.
_Avoid_: Relocation, Rename

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

**User**:
The human principal that holds access, with one global role and per-Vault roles.
_Avoid_: Account, Member

**Identity**:
An immutable external login owned by exactly one User, identified by its issuing
provider and that provider's stable subject.
_Avoid_: Email, Login, Account

**Invite**:
A single-use, expiring grant that binds the next external Identity to an existing
User; it can be revoked before it is redeemed.
_Avoid_: Registration, Signup, Token

**Session**:
An expiring, revocable credential that represents one authenticated User on one
device.
_Avoid_: Cookie, Token, Login

**Break-glass Login**:
A restricted local-password sign-in for administrators from trusted networks,
used when external identity is unavailable.
_Avoid_: Local login, Password login, Admin login

**Reauthentication**:
A fresh proof of the acting User's identity, required within a recent window
before sensitive actions.
_Avoid_: Login, Re-login

**Trusted Proxy**:
A configured upstream whose forwarded client data (address, scheme, host) the
application is allowed to believe.
_Avoid_: Reverse proxy, Gateway
