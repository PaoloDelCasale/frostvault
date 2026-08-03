# Release Vault roots only through explicit terminal decommission

A Vault's operational availability (`enabled`) and ownership of its local root
are separate concerns. Disabling, losing a mount, removing members, or otherwise
making a Vault unavailable never releases its directory. Root occupancy is
represented only by a nullable `root_released_at`; every overlap, browser, and
Source Volume inventory decision treats a Vault as occupying its stored root
until that timestamp is atomically written.

Decommission is an explicit, durable lifecycle. The primary owner or a global
administrator must have recent Reauthentication, request an authoritative
preview, type the exact Vault name, give a reason, and submit that preview's
fingerprint. The fingerprint covers the choices, Vault/root identity and health,
filesystem inventory, Local Copy protection state, cloud provider-version
identities, active work, memberships, Jobs, and file/version/byte counts. The
start transaction re-creates the preview under the shared Source Area/Vault-root
lock and rejects stale or blocked confirmation.

Local and cloud dispositions are independent and never inferred:

- **retain Local Copies** performs no local deletion;
- **remove Local Copies** queues the existing free-space Jobs, preserving their
  verified Archive Version, S3 HEAD, stable local digest, rename-claim, restart,
  and system-policy protections;
- **retain cloud history** performs no cloud deletion and requires confirmed
  crypt recovery custody when applicable;
- **purge cloud history** queues whole-Vault permanent-purge Jobs only after
  local removal is terminal, preserving cloud-deletion enablement, exact
  provider VersionIds, typed confirmation, reason, audit, notification, and the
  configured cancellable delay.

Starting decommission moves the Vault to a persistent `decommissioning` state
without changing `enabled`. Normal scans, watchers, policies, Jobs, selection,
and catalog audits exclude that state; only Jobs explicitly created by the
operation may run. Reconciliation is idempotent and repeats after restart. A
failed or unverifiable disposition leaves the operation blocked and the root
occupied.

The terminal transaction writes the operation completion, Vault tombstone,
`decommissioned_at`, and `root_released_at` together, and only after rechecking
the enrolled root identity and the selected disposition. The Vault row, UUID,
cloud namespace, sealed crypt material, memberships, Vault Files, Path History,
Archive Version/Delete Marker catalog, Jobs, notifications, and append-only
audit history remain as evidence. A cloud purge records Archive Versions as
purged; retained catalog rows are history, not evidence that cloud bytes remain.

## Filesystem limitation

FrostVault quiesces all application work and pins release to the enrolled Linux
directory identity, but it cannot prevent an external host process from writing
inside a mounted directory. Local-removal Jobs revalidate each file immediately
before unlinking, and finalization walks the root again and refuses release if
any data remains. A host write racing after that final walk is outside the
container's locking boundary; operators must keep the source quiescent during
this deliberately destructive workflow.
