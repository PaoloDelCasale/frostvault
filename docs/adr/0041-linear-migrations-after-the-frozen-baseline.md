# Linear migrations after the frozen baseline

Status: accepted by the Owner on 2026-11-14 (issue #679). Amends
[ADR-0035](0035-database-rebuilt-from-scratch.md) as it foresaw: `0001_baseline`
froze when the Owner created the production catalog (v0.6.1), and ordinary
linear Alembic migrations resume. Every other rule of ADR-0035 stands.

#679 adds the original owner and mode to each Upload Snapshot, the first schema
change since the freeze. ADR-0035's guard accepted only a database exactly at
the single revision, so a catalog of the previous release would have been
refused by the next one.

## Decision

- **One linear chain.** `app.schema_guard.SCHEMA_REVISIONS` lists the revisions
  from `0001_baseline` on, oldest first; each revision's `down_revision` is the
  one before it. The last one is `SCHEMA_HEAD`. There are no branches and no
  downgrades: rollback is a metadata backup.
- **Start.** An empty database is initialized; a database at exactly one
  earlier revision of the chain is upgraded in place to `SCHEMA_HEAD`, in one
  transaction under the same advisory lock, so a failed migration leaves it as
  it was; a database at `SCHEMA_HEAD` is used. Anything else — a v0.5.x
  revision, an unknown one (made by a newer release), several, or objects
  without `alembic_version` — is refused before any write, as before.
- **Metadata backups.** Verification accepts a dump at any revision of the
  chain. The manual restore upgrades the restored catalog with
  `upgrade_schema` before `record_metadata_restore`, which still requires
  `SCHEMA_HEAD`. `upgrade_schema` never initializes an empty database.
- **Migrations are additive or self-contained.** A migration never needs the
  application or the filesystem; it may not invent evidence for older rows
  (new evidence columns are nullable, NULL meaning "not recorded").

## Consequences

A downgrade to a release with an earlier `SCHEMA_HEAD` refuses the upgraded
catalog; restore the metadata backup made before the upgrade with that release.
