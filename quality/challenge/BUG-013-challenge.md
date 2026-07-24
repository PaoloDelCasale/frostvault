# Challenge Gate — BUG-013

## Claim
Crypt recover ignores Archive Version object_key

## Round 1
REAL_BUG (high) — Crypt download builds rclone sources from job['path'] and ignores object_key.

## Round 2
CONFIRM — independent code path review agrees; distinct from BUG-001..011 and rejected former-003.

## Verdict
**CONFIRMED** at High severity.

## Rationale
Net-new unfiltered finding with REQ mapping (REQ-025), executable RED→GREEN evidence, and no duplicate of active tracker IDs.
