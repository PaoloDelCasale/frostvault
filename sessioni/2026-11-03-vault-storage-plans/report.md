# #421 — Vault storage plans

Base: origin/main `6f4dd5429e74a9de5b502debf6cda8a288577502` (#420).
Scope: one prefix-scoped plan per Vault, preset/custom transitions only; remove folder assignments, pins and public manual class changes. Keep verified internal copy worker for #429, without exposing its new action.

Safety: migration metadata only, no S3 calls. Collapse identical effective legacy policies; refuse heterogeneous/ambiguous plans rather than guess. Retired expiration/noncurrent actions are deliberately discarded under #418/#421, never translated into deletions. Preserve catalog content/trust and external lifecycle rules plus multipart abort.

Verification: targeted local tests only; full validation in PR CI. No production operations or backup copies.

Implementation and final verification pending.
