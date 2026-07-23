# Gate Break-glass Login to trusted networks with shared-counter backoff

Break-glass Login — the local username/password path — is the recovery door for
when external identity is unavailable, so it is the most attractive target on an
internet-facing deployment. We restrict it along three axes at once: **who**,
**from where**, and **how fast**.

**Who.** Only administrators may sign in with a local password. A non-admin, an
unknown username, and a User whose `password_hash` is NULL (every OIDC-only or
shell User) are all refused with the same generic `401`, so the endpoint leaks
nothing about which of those conditions failed.

**From where.** A request is allowed only when its client address is loopback
(`127.0.0.0/8`, `::1`) or falls inside `BREAK_GLASS_ALLOWED_CIDRS`. Loopback is
**always** in the allowed set, independent of configuration, so an operator on
the host can never lock themselves out; an empty CIDR list therefore means
loopback-only. There is deliberately no "allow all" value. A request from
outside the allowed set is refused with `403` and audited, and the login page
hides the local form entirely so the option is not even presented. For this PR
the client address is the direct socket peer; trusting proxy-forwarded client
data (`X-Forwarded-For` and friends) is the separate concern of the
host/forwarded hardening work and is intentionally not done here.

**How fast.** Repeated failures are throttled by an `auth_backoff(scope, key,
failure_count, next_allowed_at, updated_at)` table. Break-glass Login counts
against both an `ip` key and an `account` key; invite redemption counts against
an `ip` key. After 5 consecutive failures the wait doubles from 30s up to a
15-minute ceiling — there is **no permanent lockout** — counters decay after an
hour of quiet, and a success clears them. Because throttle rows must outlive a
rejected request, the counter is written and the transaction committed *before*
the handler raises its `4xx`; raising inside the unit-of-work would roll the
record back.

## Consequences

The `ip` scope is shared between Break-glass Login and invite redemption on
purpose: an address abusing either path is throttled on both, which is the
desired posture for a small closed deployment. Invite redemption only counts a
*completely unknown* token as a guess, so a legitimate holder of an expired or
already-redeemed Invite is never punished. Throttle trips and denied break-glass
attempts are emitted through the structured `app/audit.py` helper now; turning
those audit records into real operator notifications (email/webhook) is deferred
to issue #16, on which this alerting depends. Misconfiguring
`BREAK_GLASS_ALLOWED_CIDRS` with an unparseable entry fails startup closed rather
than silently widening or narrowing access.
