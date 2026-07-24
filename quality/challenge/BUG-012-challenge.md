# Challenge Gate — BUG-012

## Claim
Recover overwrites on-disk Local Copy when catalog says absent

## Round 1
REAL_BUG (high) — process_recover gates only on catalog local_presence then Path.replace without on-disk check.

## Round 2
CONFIRM — independent code path review agrees; distinct from BUG-001..011 and rejected former-003.

## Verdict
**CONFIRMED** at High severity.

## Rationale
Net-new unfiltered finding with REQ mapping (REQ-024), executable RED→GREEN evidence, and no duplicate of active tracker IDs.
