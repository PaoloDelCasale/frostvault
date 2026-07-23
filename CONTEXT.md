# FrostVault

This context tracks logical files in isolated vaults and preserves recoverable
cloud history while local copies can appear, change, or be removed.

## Language

**Vault**:
An isolated archive owned and shared as one policy and storage namespace.
_Avoid_: Bucket, folder, account

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
User.
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
