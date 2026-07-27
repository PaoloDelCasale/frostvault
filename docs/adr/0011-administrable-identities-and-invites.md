# Administer Identities and Invites without ever weakening sign-in

Administrators can now inspect the Identities linked to any User, unlink one of
them, list pending Invites and revoke an Invite before it is redeemed. Every one
of those actions is a *removal*: an Identity binding is deleted, never
re-pointed at another issuer or subject, and an Invite is marked revoked rather
than rewritten or deleted, so the trail survives (ADR-0003 stays intact —
nothing here creates or re-targets a binding).

Two invariants guard the operations that could otherwise strand a principal.
Unlinking is refused when it would leave the User with neither a local password
nor any remaining Identity, because that account could never be reached again.
Demotion and deactivation share a single last-active-administrator invariant:
the change is refused when no other active administrator would remain, and an
administrator cannot demote or deactivate themselves. The invariant is enforced
inside one locked transaction and repeated as a predicate on the `UPDATE`
itself, so two concurrent requests that each look safe in isolation cannot both
commit. Invite revocation uses the same conditional-`UPDATE` strategy against
redemption: whichever statement commits first makes the other match no row.

## Consequences

The rules live in `app/services/user_administration.py`, a framework-agnostic
module, with `app/main.py` staying a thin HTTP boundary — the invariants are
directly testable without a web client, which is how the concurrency cases are
covered. Invites gain `revoked_at` and `revoked_by` (migration
`0026_invite_revocation`); pending-Invite listings exclude revoked rows and
never expose `token_hash` or a raw token. Every mutation records an admin audit
event (`admin_user_created`, `admin_user_role_changed`,
`admin_identity_unlinked`, `admin_invite_revoked`), and all mutating endpoints
require Reauthentication. The last-administrator invariant means an installation
can never be left with zero active administrators, at the cost of refusing an
otherwise-valid demotion — the remedy is to promote someone first.
