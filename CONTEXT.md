# FrostVault

This context tracks logical files in isolated vaults. Local Copies can appear,
change, or be removed. Each Vault keeps only the last verified cloud content
of each Vault File.

## Language

**Vault**:
An isolated archive owned by the Owner as one policy and storage namespace.
_Avoid_: Bucket, folder, account

**Storage Plan**:
The one Vault-wide schedule of progressively colder cloud storage classes,
chosen for access time and cost without expiring cloud content. The Owner can
move eligible Current Snapshots early to a colder class on that same plan.
_Avoid_: Folder policy, Lifecycle pin, Retention profile

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

**Reconnect Folder**:
The Owner's explicit acceptance of a local folder and its Source Volume for an
existing Vault, with a complete reconciliation before local protection resumes.
_Avoid_: Relocation, Rebind, Migration

**Recovery Kit**:
The installation's independently custodied metadata key, cloud location and
manual recovery instructions, without any catalog contents.
_Avoid_: Vault export, Per-Vault recovery file

**Kit Fingerprint**:
The short public identity of the current Recovery Kit, to compare with the
independently stored kit and its one custody acknowledgment.
_Avoid_: Master key, Checksum file

**Stop Protection**:
The Owner's deliberate end to a folder's automatic protection, keeping all Local
Copies and choosing either retained cloud content or Delayed Cloud Deletion.
_Avoid_: Decommission, Delete Vault, Disable

**Delayed Cloud Deletion**:
The Owner's explicit permanent removal of Cloud-only content after a seven-day
cancellable delay; it never removes Local Copies or follows an SMB deletion.
_Avoid_: Purge now, Hide, Sync deletion

**Vault Tombstone**:
The non-operational Vault and retained identity/history after Stop Protection
releases its root.
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
An immutable internal identity for one cloud representation of a Vault File's
content, whether still recoverable or superseded.
_Avoid_: Object, backup, revision

**Current Snapshot**:
The single recoverable cloud representation of a Vault File: its last verified
content, retained until a replacement is verified.
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
