# Uploads share one scheduling tier across Vaults

Status: accepted by the Owner on 2026-11-13 (issue #618). Amends points 2, 3, 4
and 11 of [ADR-0026](0026-fair-job-queue-and-removed-file-closure.md) only as
stated here; its windows, accounting, claim locking and deferral are unchanged.

ADR-0026 ranked every Vault's turn by one tier list: Owner operations, then
uploads the Owner asked for, then automatic work. A large manual upload in one
Vault (16,000 small files in v0.6.1) therefore held the automatic uploads of
every other Vault for its whole duration. Aging gave the waiting Vault **one**
claim after 900 seconds and then reset its credit, so 20 new files took one
Job every 15 minutes. The Owner asked for a manual upload in that Vault, not for
the protection of the other Vaults to stop.

## Decision

1. **Two orders, one per scope.**
   - *Inside a Vault* the base tiers of ADR-0026 still apply: Owner operations,
     then uploads the Owner asked for, then automatic work (automatic uploads
     and automatic free-space cleanups), oldest first within a tier.
   - *Across Vaults* there are two tiers: Owner operations (restore, free-space,
     rename, storage-class, cloud purge, Stop Protection purges) first, then one
     shared tier for every upload, manual or automatic, and every other
     automatic Job. Inside that tier Vaults are served by the existing fair
     sharing rules: fewer live leases plus nominations in this pass, then
     longer accounted unserved time, then least-recent service.
2. **The aged-turn exception is unchanged.** A turn reaches the Owner-operation
   tier exactly when it did before: a manual turn after 900 seconds of
   accounted unserved time, an automatic one after 1800. The intermediate step
   of an automatic turn (tier 2 to 1 after 900 seconds) no longer gives it
   anything across Vaults, because it already competes in the shared tier; only
   a real promotion past its cross-Vault tier uses the generation fence.
3. **No reserved slots, no per-Vault quota.** A Vault with only automatic work
   gets its share of the workers because its turn competes on equal terms, not
   because capacity is set aside. With one Vault holding every worker and
   another arriving with automatic work, each freed slot goes to the newcomer
   until both hold about half of the workers.
4. **Any Owner cancellation of an upload holds automatic admission.** An
   upload the Owner cancelled (bulk cancel of unstarted uploads, single cancel,
   or cancellation of a running upload), manual or automatic, keeps the file out
   of automatic re-queueing until its content is observed again, like a failed
   or cancelled automatic upload already did. A manual upload that failed, or
   that closed for another reason (removed file, metadata restore), does not.
5. **One rule in Python and SQL.** `job_scheduler.job_priority` keeps the order
   inside a Vault and the per-Vault, per-tier candidate windows;
   `job_scheduler.cross_vault_tier` is the order across Vaults. The catalog
   mirrors both in SQL, and a test checks the mirrors against the Python side.

## Consequences

- A manual bulk upload in one Vault no longer stops automatic protection in
  the others; it shares the workers with them instead of owning all of them.
  A manual upload now competes with other Vaults' automatic uploads, so it can
  finish later than before when other Vaults are busy.
- Inside a Vault nothing changes: its Owner operations and Owner uploads never
  fall behind its own automatic work.
- Cancelling an upload is a decision about the file until it changes; automatic
  protection does not undo it at the next scan or restart.
