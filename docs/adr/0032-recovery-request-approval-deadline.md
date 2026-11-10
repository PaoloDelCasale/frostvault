# Recovery requests have one approval decision and a cancellation deadline

Issue #536 completes the request-level Restore Approval from #533/#534.
A selection of files and/or folders inside one Vault is one recovery request.
Overlapping selections include each Vault File once. Cross-Vault selections are
partitioned into independent requests, preserving explicit Vault authorization
and isolation (ADR-0022). Folder and single-file API consumers remain supported.

`RESTORE_APPROVAL_HOLD_SECONDS` is the maximum time to answer a high-impact
request, measured from admission, not a delay before automatic execution.
At `pending_until` (inclusive), the entire still-pending request is cancelled
with a localized reason. No cloud API call or automatic approval is involved.
Approval, cancellation and expiration serialize on the same Vault lock; approval
must recheck the deadline after acquiring that lock. An accepted approval clears
the deadline. Already approved/restoring work is never expired or re-evaluated.

Entering pending approval emits one request-level notification through the
existing inbox and optional SMTP channel, deduplicated by Vault and group, never
per file. Notification failures must not roll back recovery admission.

Cost Estimates and admission share the same selection scope and whole-request
threshold calculation. Estimates are advisory: admission re-evaluates current
catalog state atomically before any Job can become runnable.
