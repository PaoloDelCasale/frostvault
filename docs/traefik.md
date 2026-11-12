# Traefik reference deployment

Use `compose.traefik.yaml` when Traefik terminates TLS in front of FrostVault.
The FrostVault service stays on a private Docker network and is **not** published
with host `ports`.

## Layout

- External network `proxy` — shared with Traefik
- Internal network `frostvault_internal` — optional private backend network
- Labels enable the router on `websecure`, request TLS via `letsencrypt`, attach
  HSTS / security-header middleware, and apply a basic rate limit
- `COOKIE_SECURE=true` and `ALLOWED_HOSTS` = public hostname
- **Two explicit proxy allowlists:** `TRUSTED_PROXIES` for the application and
  `FORWARDED_ALLOW_IPS` for Uvicorn. Configure both to the same narrow set of
  actual proxy addresses/CIDRs. Neither is automatically derived from the other.

## Two trust layers, not two independent safety barriers

The image runs Uvicorn with proxy-header processing enabled. Uvicorn reads
`FORWARDED_ALLOW_IPS` from the environment: an unset value defaults to loopback
(`127.0.0.1,::1` in the pinned release); an empty value trusts no proxy. For a
trusted **socket peer**, it consumes `X-Forwarded-Proto` and rewrites the ASGI
scheme, and consumes `X-Forwarded-For` to rewrite the ASGI client address. It
walks the address chain from the right, stripping trusted proxy hops until the
first untrusted address. Original headers remain available to the app.

FrostVault's `TRUSTED_PROXIES` controls its own client-IP resolver: it only walks
`X-Forwarded-For` if the **ASGI client it receives** is trusted, otherwise it
uses that client directly. With aligned lists, Uvicorn normally already selected
the untrusted client and FrostVault retains it. The app does **not** implement
`X-Forwarded-Proto` processing, and cannot recover the socket peer or veto a
rewrite already accepted by Uvicorn. Host checks continue to use `Host`, not
`X-Forwarded-Host`; keep `ALLOWED_HOSTS` set to the public hostname.

Consequences:

- Only `TRUSTED_PROXIES` set: the app can resolve client IPs, but Uvicorn may
  ignore the proxy's scheme. OIDC then sends `redirect_uri=http://...` and the
  provider rejects it. `COOKIE_SECURE=true` sets cookie flags; it does not turn
  request URLs into HTTPS. `APP_PUBLIC_URL` is for email links, not OIDC redirects.
- Only `FORWARDED_ALLOW_IPS` set, or a broader Uvicorn list: Uvicorn can already
  trust a peer and rewrite both scheme and client IP even if the app list would
  reject that socket peer. This is **unsafe disagreement**, not defence in depth.
- A broader app list can reinterpret a chain that Uvicorn stopped walking.
  Keep **all hops**, not just the nearest proxy, coherent in both lists. A
  narrower list at one layer does not compensate for excess trust at the other.
- A peer outside **both** lists cannot force HTTPS or claim a loopback/LAN
  address with forwarded headers. The Emergency Password remains restricted to
  loopback and `BREAK_GLASS_ALLOWED_CIDRS` (empty means loopback-only).

Do not use `*`, `0.0.0.0/0`, `::/0`, all Docker networks (such as
`172.16.0.0/12`), or an entire LAN as a workaround. Proxy trust identifies
header-writing infrastructure, **not** allowed Emergency Password clients.
Both layers must trust only infrastructure you control; a trusted proxy can
assert client data by design. It must overwrite/sanitize incoming scheme
headers and append the real connecting address to `X-Forwarded-For` (or replace
it with that address), never pass an arbitrary client's claimed identity through.
Keep proxy addresses out of Emergency Password client CIDRs: absent/unusable
forwarded client data can leave the proxy address as the client. A loopback
proxy is itself in the always-allowed loopback range, so correct header handling
is essential there too.

## Select the addresses for your topology

Inspect the source IP of the proxy connection **as seen by FrostVault**, not the
browser's address or merely Traefik's public address. Use existing network
configuration or an authorized packet capture; do not broaden trust while
trying addresses.

- **Traefik container on `proxy`:** use its stable container IP (`/32` for IPv4,
  `/128` for IPv6). Inspect the network attachment with
  `docker network inspect proxy`. A dedicated proxy-only subnet can be used if
  every address in it is controlled proxy infrastructure, never arbitrary apps.
  Ensure the selected addresses remain valid after container recreation.
- **Traefik in host networking, FrostVault on a Docker bridge:** FrostVault may
  see the bridge gateway/host address, not `127.0.0.1`. Inspect the actual source
  before setting both lists. Trusting that address also trusts any other process
  connecting with the same source IP; use network isolation if that is too broad.
- **Multiple proxies / IPv6:** list only the controlled hops, comma-separated,
  in both settings. Every upstream must sanitize headers and trust only its own
  known upstreams. Do not copy example addresses into a different topology.

## Bring-up sketch

First configure the database, Owner, OIDC provider, storage and directory
permissions from the README and `.env.example`. The commands below are an
operator-run deployment sketch, not a discovery mechanism. The address is
**illustrative only**; replace it with the verified stable proxy source address.

```bash
docker network create proxy
# Start Traefik with --providers.docker and entrypoint websecure separately.
export ALLOWED_HOSTS=frostvault.example.com
export TRUSTED_PROXIES=172.30.0.2/32
export FORWARDED_ALLOW_IPS=172.30.0.2/32
export SOURCES_ROOT=/srv/frostvault/sources
docker compose -f compose.traefik.yaml config --quiet
docker compose -f compose.traefik.yaml pull
docker compose -f compose.traefik.yaml up -d
```

Alternatively set **both** values explicitly in `.env`. The reference Compose
requires non-empty values for both, with no network-wide defaults; it does not
validate equivalence or automatically derive either value. Review them before
starting. Avoid printing a rendered Compose configuration: it may contain secrets.

On start an empty database is initialized; a database at another schema
revision is refused before any write (ADR-0035).

Confirm there is no `ports:` mapping on `frostvault`. Reach the panel only
through Traefik. Local development can keep using `compose.yaml`, which binds
`127.0.0.1:${APP_PORT:-8080}:8080` for loopback access. Both compose files pull
`ghcr.io/paolodelcasale/frostvault:latest`.

## Verify before relying on sign-in

After an authorized deployment/reload with the correct environment:

1. Register the exact `https://<public-host>/auth/oidc/callback` URI at the
   configured OIDC provider. Open `https://<public-host>/auth/oidc/login` through
   Traefik and inspect the first redirect in browser developer tools. Its
   `redirect_uri` query value must be that **HTTPS** callback, with the allowed
   public host; complete sign-in. Do not publish the authorization URL/state,
   cookies, provider credentials or callback codes.
2. From outside loopback and the configured LAN, `POST /api/login` must return
   `403` even with a claimed loopback/LAN `X-Forwarded-For`. From an explicitly
   allowed LAN, the Owner's Emergency Password should work. Do not widen
   `BREAK_GLASS_ALLOWED_CIDRS` or the proxy lists to make the outside test pass.
3. Test direct/untrusted peers only in an isolated test deployment (do **not**
   publish the production backend to test it). Forged `X-Forwarded-Proto: https`
   must not change its HTTP OIDC callback, and forged loopback/LAN client headers
   must not admit the Emergency Password. A mismatched Host must still fail.

The repository's targeted `tests.test_forwarded_proxy` runs the real Uvicorn
configuration/middleware and FrostVault routes in-process without real network
calls or a database. PostgreSQL-backed forwarded OIDC/Emergency Password route
regressions also run in PR CI. These do not replace verifying your actual proxy
source address and header sanitization. If the callback is still HTTP, inspect
Uvicorn's allowlist and the observed source; if IP gating differs, compare both
lists and all header-writing hops. Stop on disagreement; never enable wildcard
trust as a diagnostic fix.

## Headers and limits

The reference labels set:

- HSTS (`stsSeconds=31536000`, includeSubdomains, preload)
- `X-Content-Type-Options`, XSS filter, `Referrer-Policy`, `X-Frame-Options=DENY`
- A restrictive `Content-Security-Policy` suitable for the self-hosted UI
- Rate limit average 100 / burst 200 (tune for your edge)

Adjust middleware names or CSP if you front additional assets. Preserve existing
routing/authentication middleware when adapting this reference deployment.

## Public HTTP surface

The reference Host-only router forwards paths to FrostVault; it is **not** a
path allowlist or an authentication layer. Owner API access still requires the
application's Session. Preserve OIDC/login routes and existing authentication,
security-header and rate-limit middleware when adapting the router.

- Keep `API_DOCS_ENABLED=false` in production: `/docs`, `/redoc`, `/openapi.json`
  and Swagger's `/docs/oauth2-redirect` return 404 by default. Opt-in is anonymous
  and intended only for isolated development, not as an Owner-only docs page.
- `/metrics` returns 404 unless `METRICS_TOKEN` is configured, then requires its
  independent Bearer credential (missing/malformed/invalid = 401). A browser
  Session never grants scraper access. Prefer scraping privately, preserving an
  authorized Host and existing narrow proxy trust. If using the existing HTTPS
  router, ensure it forwards `Authorization` without logging it. Do not add a
  public port/router or weaken authentication middleware for a scraper.
- `/health` and `/ready` remain anonymous minimal probes; readiness exposes only
  status/dependency booleans, not workload counts or queue state. If external
  monitoring does not need them, deny them at the public edge along with metrics
  and docs/schema paths (including their subpaths). This is an optional operator
  restriction, not a change to the reference router or an app auth bypass.

Public denials must leave the image's **internal loopback** readiness probe
working; it never passes through Traefik. Do not add a Host or authentication
exception in FrostVault. For the scraper credential-file example and upgrade
steps, see [HTTP diagnostics](../README.md#http-diagnostics). No secret value is
provided in the examples; provisioning and any production rollout are separate
operator actions.

## Local readiness behind Traefik

Both Compose templates inherit the image's Python healthcheck; no public port,
Traefik route or authentication exception is required for it. The probe connects
to `127.0.0.1:8080/ready` **inside FrostVault**, sends an exact authorized `Host`
from `ALLOWED_HOSTS`, and does not go through Traefik, external DNS, TLS or
forwarded-header processing. Keep `ALLOWED_HOSTS` set to the intended public
hostname; do not add `127.0.0.1` merely for healthchecks. The reference router
label expects one public hostname; the probe also supports a comma-separated
allowlist, but changing public routing for multiple names is a separate task.

```bash
# Read-only: same probe as the Docker healthcheck, no hardcoded public Host.
docker compose -f compose.traefik.yaml exec -T frostvault python -m app.readiness_probe
docker inspect --format '{{json .State.Health}}' frostvault
```

A successful probe means local dependency-aware readiness, **not** proof that
Traefik routing, TLS or OIDC works. A database/worker/configuration/Source Volume
layout 503 must remain unhealthy even if `/health` says the process is alive.
Host validation still rejects unauthorized hosts for both endpoints and for
external requests. See [container readiness](../README.md#container-readiness)
for Host selection rules, timeouts, startup grace and failure diagnosis.
