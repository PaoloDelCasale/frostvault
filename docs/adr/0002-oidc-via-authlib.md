# Depend on Authlib for OIDC at the library level

Generic OpenID Connect login uses Authlib (with cryptography) as a JOSE and
OAuth 2.0 toolkit, not as a framework integration. We call Authlib to build the
Authorization Code + PKCE (S256) request and to verify id_token signatures
against the provider's JWKS, but the application owns the flow: the transient
login state (state, nonce, code_verifier, return_to, invite_id) lives in the
server-side `oidc_login` table with a short TTL and is swept, and every id_token
claim we care about (`iss`, `aud`, `exp`, `nonce`, `at_hash`) is validated
explicitly in `app/oidc.py`.

We deliberately do **not** use Authlib's Starlette client. That integration
keeps the login transaction in the signed session cookie and hides validation
behind framework glue, which conflicts with our server-side Session model and
with keeping OIDC a deep module that returns validated claims and nothing else.
Hand-rolling JOSE/JWKS/PKCE instead would mean owning signature verification and
key rotation — security-critical code that Authlib already implements and
maintains.

## Consequences

Authlib and cryptography become pinned runtime dependencies. `app/oidc.py`
exposes a small interface (`begin_login`, `complete_login`, `sweep_expired_logins`)
with HTTP injected for testing, so a fake OIDC provider (discovery + JWKS +
signed id_token) can exercise the happy path and the replay, nonce, state, PKCE,
audience, issuer, expiry, and at_hash failures. Identity binding (invites,
self-link) and login-UI concerns are handled in later PRs; this PR only resolves
an already-linked Identity to a User. `authlib.jose` is deprecated in favour of
`joserfc` but supported until Authlib 2.0; migrating the JOSE calls is a
low-risk future change that does not affect this decision.
