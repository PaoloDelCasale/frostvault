# Challenge Gate — BUG-014

## Claim
User reactivation omits session_version bump

## Round 1
REAL_BUG (high) — update_user bumps session_version only on password change, not active transitions.

## Round 2
CONFIRM — independent code path review agrees; distinct from BUG-001..011 and rejected former-003.

## Verdict
**CONFIRMED** at High severity.

## Rationale
Net-new unfiltered finding with REQ mapping (REQ-026), executable RED→GREEN evidence, and no duplicate of active tracker IDs.
