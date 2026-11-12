# Rebuild the database from scratch for v0.6

Status: accepted by the Owner on 2026-11-12 (issue #578). Amends ADR-0016,
ADR-0017 and ADR-0034 only as stated here; their safety and recovery guarantees
otherwise remain in force.

The schema inherited from v0.5.x still has the shape of multi-user and
version-history models that ADR-0016 (Current Snapshot) and ADR-0017 (single
Owner) removed. The Owner is the only developer and the only user, and no
catalog has to be kept. We therefore rebuild the database from scratch instead
of migrating it, and accept breaking every existing installation.

## Decision

- **No migrations, no compatibility.** The 75 historical Alembic revisions,
  metadata-backup upgrade from old schemas, runtime schema probes and legacy
  code paths are deleted, not ported. One Alembic revision, `0001_baseline`,
  holds the whole schema and is edited in place during development. It is
  frozen when the Owner recreates the database for real use; from then on
  Alembic resumes ordinary linear migrations.
- **Simple guard.** A nonempty database whose revision is not the expected head
  is refused, with a clear message, by app start and by metadata restore, before
  any mutation. An old catalog is never adopted, stamped, upgraded or reset;
  an old metadata backup is refused. Only a truly empty database is initialized.
  No import of old Vault objects: new Vaults use new namespaces.
- **Single Owner in the schema.** One Owner row, constrained to be the only one;
  no users, members or per-user attribution columns. Actors are Owner,
  emergency or system.
- **Current Snapshot in the schema.** S3 bucket versioning and readable
  `vaults/<uuid>/<path>` keys stay (ADR-0018): versioning is what keeps the old
  copy until its replacement is verified. The database keeps at most the Current
  Archive Version and one replacement in progress per Vault File, referenced
  explicitly from the file, plus versions with open cleanup or remediation
  work. A superseded version is deleted from S3 by exact VersionId and its row is
  removed together with its completed cleanup proof. There is no version
  numbering.
- **Development on `main`.** The rebuild lands directly on `main`; the app and
  CI may be broken until the porting is complete. Full gates must be green only
  at the end, before release.
- **Upload safety unchanged.** Committed intent/send-started before provider
  effects, immediate independent receipt journaling, frozen expected integrity
  proof, exact ambiguous-send reconciliation, verified replacement before
  cleanup, completion only after cleanup, DB retries never replaying provider or
  filesystem effects. Pause stays a per-file claim boundary; generations never
  replace live lease, cancellation, root or filesystem checks. One independent
  S3 object per file (no packs); #573 mtime preserved. AWS S3 is the supported
  provider. Conditional PUT is not adopted: HTTP 412 is not proof of an earlier
  send and an ETag is not a VersionId.
- **Retention.** Terminal Jobs and read/resolved notifications are deleted after
  30 days, audit after 90 (configurable), never while referenced by open work.
  Retention never deletes local or cloud content.
- **Acceptance.** A small-file upload, completed after cleanup, costs at most
  15 DB transactions and 120 SQL statements, and the cost does not grow with
  accumulated history. This is an automated test, not a comparison with v0.5.5.
- **Release.** v0.6.0 is an ordinary release with a compatibility warning; it
  moves `latest`.

## Considered options

Removing S3 versioning with immutable per-snapshot keys would make renames
DB-only and drop delete markers and version listings, but the bucket would no
longer be browsable by path for emergency recovery. Overwriting path keys
without versioning would lose the old copy before the new one is verified.
