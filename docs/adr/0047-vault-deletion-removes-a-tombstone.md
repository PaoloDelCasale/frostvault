# Vault Deletion removes a Tombstone

Status: accepted by the Owner on 2026-11-16 (issue #717). Adds a step after
Stop Protection; Delayed Cloud Deletion and its Cloud-only gate are unchanged.

## Context

A Vault stopped with retained cloud content becomes a Vault Tombstone and,
until now, stayed forever: listed, in the catalog, with its objects in S3.
Delayed Cloud Deletion cannot help, because it requires verified local
absence and a Tombstone's root is no longer observed. Deleting the prefix by
hand or the rows in SQL leaves the catalog inconsistent.

## Decision

- **Only a Tombstone can be deleted.** An active Vault first stops with
  retained cloud content; there is no single "stop and delete" flow.
- **Preview from a live listing** of `vaults/{uuid}/`: objects, versions,
  delete markers, bytes and the early-deletion cost of GLACIER/DEEP_ARCHIVE
  versions. No listing, no confirmation.
- **Confirmation** with the exact Vault name, a reason and a fresh
  authentication, then the same seven-day cancellable delay. The dialog says
  that Local Copies stay where they are but lose their cloud copy.
- **The whole prefix goes**: every version and delete marker under
  `vaults/{uuid}/`, catalogued or not. The prefix belongs to that Vault only;
  no key outside it is ever touched. The Cloud-only gate does not apply:
  nothing local is removed.
- **Removal only after proof**: when a fresh listing of the prefix is empty,
  the Vault and all its catalog rows leave the database; one audit event
  `folder_protection.vault_deleted` keeps UUID, name, counts and actor.
- **Failure keeps the Tombstone**: a failed or incomplete deletion leaves the
  Vault in the catalog with an open Problem (the exception to closing the
  Problems of stopped Vaults, #715); the Owner may retry without a new delay,
  or cancel.

## Consequences

The Tombstone is no longer the end of a Vault's life; history survives only
in the audit log and older metadata backups. Recovery of a deleted Vault's
cloud content is impossible by design.

## Amendment: the early-deletion cost counts from the Class Entry (#731, 2026-11-17)

A version enters DEEP_ARCHIVE days or months after its upload when a Storage
Plan transitions it, so `LastModified` overstated how much of the minimum had
passed. The preview now counts each listed version's Minimum Storage Duration
from its Class Entry: the recorded `storage_class_since` while the catalog
records the same class the listing shows; otherwise an estimate from the
Storage Plan and the version's creation (the catalog's `created_at`, or
`LastModified` for a version the catalog never knew), counted as estimated.
The preview also states how many versions and bytes are still within their
minimum, and from when deleting them would start to save.

