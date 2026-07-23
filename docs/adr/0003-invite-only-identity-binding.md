# Bind external Identities to existing Users by invite only

Every external Identity is attached to a pre-existing User through a single-use,
expiring Invite, and never provisioned automatically from an id_token. An OIDC
callback that resolves an unknown `(issuer, subject)` pair signs nobody in unless
that login carried a valid Invite (or the acting User self-links their own
Identity). We deliberately do **not** trust the `email` claim, or any other
claim, to discover or create a User: emails are reassignable and not guaranteed
verified across providers, so email-based auto-provisioning would let whoever
controls an address at the issuer inherit an existing User's Vault access.

An Invite records only the target User, the issuer/subject it eventually bound,
and an expiry; the raw token is shown once and stored as a SHA-256 hash. Binding
enforces `UNIQUE(issuer, subject)`, so one external Identity can map to at most
one User and cannot be silently re-pointed. The self-link path is the same
mechanism turned inward: an authenticated User issues an Invite for themselves
and redeems it, which lets a bootstrap administrator sign in via Break-glass
Login and then attach their external Identity without any special-case code.

## Consequences

Onboarding is a two-step admin action — create a (now passwordless) shell User,
then issue an Invite — rather than self-service signup, which is the intended
posture for a small, closed, internet-facing deployment. `users.password_hash`
becomes nullable so OIDC-only and shell Users carry no local password; Break-glass
Login must reject a null hash. Invites and identity binding live in `app/invites.py`
behind a small interface (`create_invite`, `resolve_invite`, `redeem_invite`,
`link_identity`); the OIDC callback stays a thin resolver that either finds a
linked Identity or redeems the Invite carried through the `oidc_login` row. Losing
an Invite before redemption simply means the admin issues another; there is no
recovery path that bypasses an Invite.
