# Harden host/origin/forwarded handling, CSRF, and step-up reauthentication

This final PR of the server-side session work closes the perimeter around the
now purely server-side Session. It hardens three edges at once — the **network
edge** (which Host/Origin/forwarded data we trust), the **request edge** (proving
a mutating request came from our own frontend), and the **action edge** (proving
a recent, deliberate human is behind a sensitive action).

**Host, origin, and forwarded edge.** `TrustedHostMiddleware` answers only for
the names in `ALLOWED_HOSTS`; allowed origins are derived from those hosts plus
the request scheme, and mutating requests carrying a cross-origin `Origin` header
are refused as defence in depth. `return_to` is an open-redirect guard: it
accepts only a local path (must start with a single `/`, never `//`, `/\`, or a
backslash/control character), so the OIDC round-trip can never bounce a user to
an attacker's site. The application client IP is resolved by `app/proxy.py`: it walks
`X-Forwarded-For` only if the ASGI client it receives is inside
`TRUSTED_PROXIES` (CIDRs), stripping trusted hops from the right until the first
untrusted address; otherwise that ASGI client is used directly. In the deployed
stack this is not necessarily the original socket peer (see the #462
clarification below). This promotes PR4's socket-peer-only network gate to a
proxy-aware one for the Owner's Emergency Password.

**Forwarded deployment clarification (#462).** Uvicorn independently checks the
socket peer against `FORWARDED_ALLOW_IPS`, then processes `X-Forwarded-Proto`
for the scheme and `X-Forwarded-For` for the ASGI client **before** calling the
app. FrostVault does not process forwarded scheme or recover the original peer;
its allowlist cannot veto a rewrite already accepted by Uvicorn. The Owner chose
two explicit, narrow and coherent proxy-only allowlists, not automatic
derivation or wildcard trust. Set both lists to the same actual proxy hops as
seen by FrostVault. Broader trust at either layer is unsafe, not compensated by
a narrower list at the other. The proxy must sanitize scheme and append/replace
client headers with the real connecting address. See
[the deployment and verification guide](../traefik.md). Host/origin rules and
the loopback/allowed-LAN Emergency Password gate remain unchanged.

**CSRF edge.** Every Session already owns a per-session `csrf_token`
(synchronizer token, created in PR1's schema). It is exposed to the frontend
through `/api/me` and a JavaScript-readable `frostvault_csrf` cookie, and required back
as an `X-CSRF-Token` header on every `POST`/`PUT`/`PATCH`/`DELETE`. The check is
constant-time and the token is compared against the server-side Session, so a
stolen cookie alone is useless. `POST /api/login` is exempt because there is no
Session yet; the OIDC `state` parameter plays the same role for the login/callback
round-trip.

**Reauthentication edge.** Sensitive actions — since ADR-0017 only the
Recovery Kit download and deleting from the cloud — require a `reauth_at` no older than
`REAUTH_WINDOW_SECONDS` (default 10 minutes). The `require_recent_reauth`
dependency raises a `403 {"error": "reauth_required"}` marker that the frontend
keys on to trigger a **step-up with the user's own method**: OIDC users bounce
through the provider with `prompt=login` (the callback refreshes `reauth_at` and
rotates the existing Session rather than minting a new one), while the Owner on
the LAN re-enters the Emergency Password at `POST /api/reauth`. A fresh login already counts as a
recent reauthentication, so the window only bites on long-lived sessions.

## Consequences

Production is fail-closed: with `COOKIE_SECURE=true` the app refuses to start
unless `ALLOWED_HOSTS` and `TRUSTED_PROXIES` are set, and unless a partially
configured OIDC provider has issuer, client id, and secret together. The dead
`SESSION_SECRET`/`SessionMiddleware` configuration from the pre-PR1 signed-cookie
store is removed, so there is a single source of session truth. Because CSRF is
now mandatory on mutations, any client of the JSON API — including the bundled
`app.js`/`admin.js` and the `no_vault.html` logout — must send the header;
callers that do not will receive a `403`. The step-up marker is a contract with
the frontend: back-end callers should treat `403 {"error":"reauth_required"}` as
"prompt for reauthentication and retry", not as a hard failure.
