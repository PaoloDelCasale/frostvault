# FrostVault

This context tracks logical files in isolated vaults. Local Copies can appear,
change, or be removed. Each Vault keeps only the last verified cloud content
of each Vault File.

## Language

**Vault**:
An isolated archive owned by the Owner as one policy and storage namespace.
"Folder" always means a directory inside a Vault, never the Vault itself.
_Avoid_: Bucket, folder, protected folder (cartella protetta), account

**Vault Bucket**:
The cloud bucket that holds one Vault's content. FrostVault creates a
dedicated one for every new Vault, with its content at the bucket root; a Vault
keeps its bucket for life, and only an older Vault may share a bucket under a
per-Vault prefix.
_Avoid_: Target, Namespace, Shared bucket

**System Bucket**:
The installation's own bucket for metadata backups (and the billing export),
separate from every Vault Bucket and the cloud location named by the Recovery Kit.
_Avoid_: Backup bucket, Vault Bucket

**Retention Lock**:
An optional Governance-mode minimum retention, chosen only when a Vault is
created, during which no Archive Version of its Vault Bucket can be deleted, not
even by FrostVault. Deletions that meet it are deferred to the lock's end, never
failed.
_Avoid_: Object Lock compliance, Immutability, WORM

**Retained Version**:
A superseded Archive Version kept in the cloud only because its Retention Lock has not ended; FrostVault deletes it by exact version once the lock ends.
_Avoid_: Orphan, Leftover

**Minimum Storage Duration**:
The days a storage class bills an Archive Version for once it enters that class,
counted from its Class Entry, whether or not it is deleted earlier.
_Avoid_: Retention, Early deletion fee

**Class Entry**:
When an Archive Version entered its current storage class: observed, or
estimated from the Storage Plan until a change is observed.
_Avoid_: Upload date, LastModified

**Billed Cost**:
The Vault's actual AWS charges, read from the installation's billing export, as
opposed to a Cost Estimate.
_Avoid_: Cost Estimate, Real price

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

**Root Identity**:
The versioned opaque identity of a Vault's root directory; it survives ordinary
reboots and remounts of the same folder, and on a union filesystem the folder
moving between its cache and its disks, and changes when the folder is replaced
or a different filesystem is mounted there. On a union filesystem it names the
folder by its Source Volume and path, so a folder recreated at the same path
keeps it.
_Avoid_: Marker file, Device number

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

**Paused Vault**:
A Vault whose automatic cloud work the Owner has temporarily held: nothing new
starts, running Jobs finish, the catalog keeps following Local Copies, and
recoveries continue. It stays paused until the Owner resumes it; it never
deletes or stops protection.
_Avoid_: Stopped, Suspended, Disabled, Operating window

**Manual Upload**:
A Vault whose automatic upload the Owner has turned off by choice: new and
changed Local Copies wait until the Owner uploads them. It is a setting, not a
failure or a Problem; the Vault's protection follows its files.
_Avoid_: Protection suspended, Protezione sospesa, Suspended

**Delayed Cloud Deletion**:
The Owner's explicit permanent removal of Cloud-only content after a seven-day
cancellable delay; it never removes Local Copies and never follows a deletion
on the server on its own, except through the Owner's opt-in Mirror Cleanup.
_Avoid_: Purge now, Hide, Sync deletion

**Cloud Duplicate**:
A Cloud-only Vault File whose content is still present as a Local Copy, and
protected by a verified Current Snapshot with the same content, at another path
of the same Vault. Deleting it from the cloud loses no content; a Cloud-only
file without such a copy is the only remaining copy of its content.
_Avoid_: Orphan, Leftover, Copy, Conflict

**Automatic Duplicate Cleanup**:
An opt-in, per-Vault setting under which FrostVault itself schedules, once a
day, the Cloud Duplicates whose Local Copy was last seen long enough ago as one
Delayed Cloud Deletion, with the same delay and cancellation. It never schedules
a Cloud-only file without such a copy, and above its cap it schedules nothing
and asks the Owner to confirm by hand.
_Avoid_: Auto-purge, Sync deletion, Deduplication, Mirror Cleanup

**Mirror Cleanup**:
An opt-in, per-Vault setting under which FrostVault itself schedules, once a
day, the Cloud-only files whose Local Copy was last seen long enough ago,
optionally only under chosen folders, as one Delayed Cloud Deletion, with the
same delay and cancellation. Unlike Automatic Duplicate Cleanup it deletes the
only remaining copy of that content, so the cloud then mirrors the server and
no longer protects against a deletion on the server; it suits copies that are
already backups. A file never seen locally is never scheduled, and above its
cap it schedules nothing and asks the Owner to confirm by hand.
_Avoid_: Sync deletion, Auto-purge, Automatic Duplicate Cleanup, Retention

**Vault Tombstone**:
The non-operational Vault and retained identity/history after Stop Protection
releases its root.
_Avoid_: Deleted Vault, Active Vault

**Vault Deletion**:
The Owner's explicit, delayed and cancellable removal of a Vault Tombstone:
all of its cloud content goes, then the Vault leaves the catalog and only an
audit record of it remains. Local Copies are never touched.
_Avoid_: Forget Vault, Delayed Cloud Deletion, Stop Protection

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

**Source Device**:
The disk a Local Copy's bytes are read from, which may differ from file to
file inside one Source Volume (for example each array disk behind an Unraid
user share). Reads of Local Copies are spread across Source Devices and
limited per device; a wrong guess of a file's device only makes reading
slower, never changes what is read or protected.
_Avoid_: Source Volume, Disk, Mount

**Archive Version**:
An immutable internal identity for one cloud representation of a Vault File's
content: the Current Snapshot, a replacement in progress, or a superseded one
kept only until its exact cleanup completes.
_Avoid_: Object, backup, revision

**Current Snapshot**:
The single recoverable cloud representation of a Vault File: its last verified
content, retained until a replacement is verified.
_Avoid_: Sync, Mirror, Unversioned, Revision

**Excluded**:
The protection state of a present Local Copy that automatic upload deliberately
skips (default noise such as `._*` and `.DS_Store`, or the Vault's own exclusion
rules). It is not pending work and never counts against a Vault's protection;
a manual upload remains possible.
_Avoid_: Ignored, Skipped, Pending

**Excluded Folder**:
A folder that one of the Vault's own exclusion rules covers entirely
(`dir/**`): neither scans nor real-time detection look inside it. Files
already known there keep their last known state as Excluded and are marked
not observed; they never become missing. Removing the rule makes the Vault
look inside again.
_Avoid_: Ignored folder, Skipped folder, Pruned folder

**Unconfirmed**:
The protection state shown for a Local Copy FrostVault cannot yet confirm,
because the Vault has no successful scan or its path could not be read. It
covers every other state except Excluded and Unsupported, which never count
against protection; the last known state underneath (for example already in
the cloud, or waiting for upload) stays visible as a detail, never as protected.
_Avoid_: Pending, Adoption, Unknown

**Stopped Upload**:
The protection state of a present Local Copy whose upload the Owner stopped: it
is kept out of automatic upload until its content changes. It is not pending
work and not protected; a manual upload remains possible.
_Avoid_: Held, Pending, Paused

**Unsupported**:
The protection state of a local entry FrostVault cannot archive: a symbolic
link, which is never followed, or a special file such as a pipe, socket or
device. It is not a Problem and never counts against a Vault's protection.
_Avoid_: Broken, Invalid, Problem

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

**Upload Snapshot**:
The fixed content and independent expected integrity evidence of one admitted
upload, distinct from a Local Copy that may change later.
_Avoid_: Current file, provider observation

**Upload Attempt**:
One send of an Upload Snapshot, with its own opaque token. Once its send has
started it is never sent again: it is reconciled, or settled as landed or as a
definitive no-write, and a new send is a new attempt of the same snapshot.
_Avoid_: Intent, retry, upload Job

**Upload Receipt**:
The provider's raw answer to an Upload Attempt, kept as a fact independent of
any Job. It never publishes or deletes anything; a receipt that contradicts
the attempt's settlement blocks reconciliation.
_Avoid_: Confirmation, proof, upload status

**Upload Verification Mode**:
The requested integrity proof for an Upload Snapshot: provider cryptographic
ingestion proof or independent full Read-back, not an Archive Version's outcome.
_Avoid_: Global setting, upload status

**Read-back**:
An independent reading of one exact Archive Version's complete plaintext compared
with its durable expected digest, with cloud retrieval and egress costs.
_Avoid_: HEAD, provider ingestion proof

**Integrity**:
The durable result of proving an Archive Version's content against independent
expected evidence: unverified, verified, or mismatch.
_Avoid_: Upload status, availability

**Availability**:
Whether an exact Archive Version can currently be located in cloud storage:
unknown, available, missing, or purged.
_Avoid_: Integrity, restore state

**Job**:
A queued or running operation targeting one stable Vault File and, when known,
one exact Archive Version.
_Avoid_: Task, request

**Recovery**:
The Owner's request to bring a Vault File's Current Snapshot back from the cloud
as a Local Copy, including any wait for a cold storage class to become readable
(the cloud restore). In the Italian UI it is always «Recupero» / «Recupera»;
«Ripristino» names only the restore of a metadata backup.
_Avoid_: Restore, Ripristino, Download

**Restore Approval**:
The Owner's explicit consent, given once for a whole recovery request, before a
request whose total size or Cost Estimate is high-impact starts any cloud
restore. Unanswered in time, the request is cancelled; once a cloud restore has
started it is never asked again.
_Avoid_: Confirmation, Per-file approval

**Scheduling Tier**:
The order in which the worker serves runnable Jobs. Inside a Vault: Owner
operations (restore, free-space, rename, storage-class, cloud purge) first, then
uploads the Owner asked for, then anything admitted automatically (automatic
uploads and cleanups). Across Vaults: Owner operations first, then one shared
tier where uploads the Owner asked for and anything admitted automatically
compete as equals. Vaults share the workers fairly inside a tier, and work that
has waited long enough moves up a tier.
_Avoid_: Priority queue, Lane

**Removed File Closure**:
The outcome of an upload whose Local Copy is provably gone before any cloud
write: the Job ends cancelled as a removed file, not failed, and raises no
failure notification.
_Avoid_: File missing error, Orphan job

**Stalled Queue**:
A queue with runnable Jobs, nothing running and no claim for five minutes.
Reported as a protected metric and an Owner problem.
_Avoid_: Deadlock, Hang

**Stalled Vault**:
A Vault whose background work (automatic admission, identifying new Local
Copies) has held it for longer than ten minutes, so scans and new uploads
wait behind it. Reported as an Owner Problem naming the work and since when; it
clears when the work ends. A full scan is never a Stalled Vault.
_Avoid_: Hung vault, Deadlock

**Server Priority**:
An optional installation-wide setting that slows FrostVault down while the
server's disks are very busy: one read per Source Device and no new uploads
until the load drops. A queue held back this way is not a Stalled Queue.
_Avoid_: Quiet hours, Throttle window

**Expected Finish**:
The time of day a Vault's work is expected to end: the later of its upload to
the cloud and its reading from the disk (fingerprint verification), each at its
average speed over the last five minutes, named with the phase that limits it.
With one phase active, that phase. Unknown until an active phase is measured.
Unknown as well when no phase has made progress for five minutes: the Vault is
then shown stopped since it last advanced. Reading an uploaded file back from
the cloud to verify it is progress. A Vault whose only work waits in the queue
is not shown stopped while the installation advances: it waits its turn.
_Avoid_: ETA, Time left, Remaining time

**Cost Estimate**:
An approximate storage or restore cost computed from the AWS list prices bundled
for the configured region. Prices are not editable and are never an AWS bill.
_Avoid_: Price book, Quote, Bill

**Upload Bandwidth Limit**:
The one optional, installation-wide cap on upload transfer speed. There are no
per-Vault quotas, limits, or operating windows (a Paused Vault is a manual
hold, not a window); recoveries are never throttled.
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
device. It names no Vault: the interface opens on the Vault of its route, else
the one remembered in the browser, else the first listed.
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
