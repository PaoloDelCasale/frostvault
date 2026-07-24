# Challenge Gate — BUG-015

## Claim
Restore estimate vs worker price-book skew

## Round 1
REAL_BUG (high) — Estimate API uses get_active_price_book; process_recover uses hardcoded defaults.

## Round 2
CONFIRM — independent code path review agrees; distinct from BUG-001..011 and rejected former-003.

## Verdict
**CONFIRMED** at High severity.

## Rationale
Net-new unfiltered finding with REQ mapping (REQ-027), executable RED→GREEN evidence, and no duplicate of active tracker IDs.
