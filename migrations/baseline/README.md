# Native baseline components — runtime cutover pending

These SQL files define the new physical current-file/evidence core. They are
**not incremental Alembic revisions and are not yet the application's schema**.
The current graph still ends at `0076_operational_retention`. Do not execute these
files against an installation, change `HEAD_SCHEMA_REVISION`, or wire a provider
send solely because `start_send` returns a row.

## Implemented component contracts

- `file_state.sql`: identity, current path and Local Copy scalars in one row;
  only closed mappings in `file_path_history`. An unobserved Local Copy is absent,
  not an invented Missing/all-null observation. UUID generations fence metadata
  publication; they do not prove a live root/claim.
- `upload_evidence.sql`: immutable expected evidence once per File/evidence hash;
  attempts reference it and freeze typed namespace/root inputs. Native UUID,
  `timestamptz`, SHA-256 `bytea`/`bytea[]`; original mtime stays signed BIGINT ns.
  Root identity codec versions remain opaque TEXT, not numeric IDs by fiat.
- Receipt facts commit separately, without a Job/Vault lock or live claim.
  Identical allowlisted facts deduplicate; contradictory, malformed and late
  acknowledgments remain representable. Neither ETag nor a receipt is success.
- Exact representations have scoped receipt/attempt/File FKs and globally unique
  provider/bucket/key/VersionId identities. No ordinal or latest-row election.
  Prior Current IDs are frozen CAS tokens, not FKs retaining a chain of obsolete
  keepers: current proof does not permanently pin closed historical proof.
- Current/pending/matched/held references are scoped to one File. Current
  selection requires verified expected bytes, exact evidence and explicit CAS;
  verification/publication rolls back as a unit without erasing its receipt.
  Another Local Copy observation wins over a stale fingerprint publication.
- `app/file_state.py` and `app/upload_evidence.py` contain DB-only operations;
  no provider/filesystem effects or effect retries occur in them.

## Required before the atomic application/baseline cutover

1. Wire actual catalog, browse, rename-confirmation, recovery and upload queries;
   eliminate old physical File/path/Local Copy tables, not mirror them or add
   compatibility views. Preserve provisional-rename proof/pending-work guards.
2. Normalize and integrate frozen inventory, multipart/reconciliation journals,
   namespace ownership/pins, exact cleanup obligations and deletion consent.
   The metadata send-start CAS alone is NOT effect admission. Copy/move/storage
   class proof must preserve independent expected plaintext and actual layout.
3. Split stable Job metadata from hot lease/status/progress state. Keep fresh
   claims, cancellation/revocation, live filesystem root checks, durable
   send-before-effect and independent receipt capture at their effect boundaries.
   Final status CAS belongs in the same outer verification/publication unit.
   Finish validated source-frame handling for an initially absent Local Copy:
   create it only after verification, without overwriting a newer observation.
   Do not upgrade a SHARE catalog capability to an exclusive one mid-unit.
4. Assemble one fresh-only declarative baseline, direct Owner creation and strict
   native JSON/API boundaries. Isolate historical migrations in tests, reject
   incompatible/nonempty/unknown states and preserve restore preflight safety.
5. Retain all live proof references/open uncertainty. `settled_at` by itself is
   not authorization to garbage-collect proof or to declare cleanup complete.
6. Verify real runtime/migration/restore/concurrency contracts and final upload
   budgets/comparisons. Component tests do NOT establish acceptance or speed.

`tests.test_file_state` / `tests.test_upload_evidence` apply these exact components
only inside disposable PostgreSQL fixtures, sharing their existing Vault parent.
They do not populate/mirror legacy File/path/Local Copy rows. The complete final
baseline will replace that temporary fixture assembly as well as the old graph.
