# #427 implementation

Base: 42beaea3e5ffffbdeb351b65ab74aad850fddbd9. Scope: explicit Reconnect folder, installation Recovery Kit, stale-metadata warning. No production services/database, delegates, merge or issue/epic updates.

Read issue #427, native parent #410/blockers #408/#424/#422 (closed), blocked #430, siblings #425/#426 and map #360/decisions #361/#364/#365/#366; source layout, relocation, snapshot, single-owner and plaintext ADRs.

Safety design:
- Reuse persistent full-scan suspension, allocation/admission/scan locks and pinned root checks. Accept a mount fingerprint for the selected Vault only, never replace shared Source Volume identity.
- Reconnect reconciles cloud first with cleanup disabled, verifies previously unknown current bytes by exact-VersionId reads, resets local fingerprints and holds all existing cloud paths before local scan. Failure leaves suspension; no cloud deletion authority.
- Non-catalog kit contains standalone recovery instructions and cloud location. It also carries the metadata decryption material. Fingerprint/custody are installation-wide and tied to the current kit; download retains reauthentication/audit/no-store.
- Manual metadata restore records manifest date; durable distinct-file cloud reconciliation counts visible in recovery status. Routine per-file alerts are not added.

Targeted local tests only; complete validation in draft PR CI. API export + generate:api required.
