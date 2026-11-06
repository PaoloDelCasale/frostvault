# Traefik reference deployment

Use `compose.traefik.yaml` when Traefik terminates TLS in front of FrostVault.
The FrostVault service stays on a private Docker network and is **not** published
with host `ports`.

## Layout

- External network `proxy` — shared with Traefik
- Internal network `frostvault_internal` — optional private backend network
- Labels enable the router on `websecure`, request TLS via `letsencrypt`, attach
  HSTS / security-header middleware, and apply a basic rate limit
- App settings: `COOKIE_SECURE=true`, `ALLOWED_HOSTS` = public hostname,
  `TRUSTED_PROXIES` = Traefik/proxy CIDR so `X-Forwarded-*` is believed only
  from that hop. This is required for the network-gated Emergency
  Password to use the real client address.

## Bring-up sketch

```bash
docker network create proxy
# Start Traefik with --providers.docker and entrypoint websecure separately.
export ALLOWED_HOSTS=frostvault.example.com
export TRUSTED_PROXIES=172.16.0.0/12
export SOURCES_ROOT=/srv/frostvault/sources
docker compose -f compose.traefik.yaml pull
docker compose -f compose.traefik.yaml up -d
```

Schema migrations run on start when `AUTO_MIGRATE=1` (default). Set
`AUTO_MIGRATE=0` and run `python -m app.backup_upgrade` before `up` for a fully
manual upgrade gate.

Confirm there is no `ports:` mapping on `frostvault`. Reach the panel only
through Traefik. Local development can keep using `compose.yaml`, which binds
`127.0.0.1:${APP_PORT:-8080}:8080` for loopback access. Both compose files pull
`ghcr.io/paolodelcasale/frostvault:latest`.

## Headers and limits

The reference labels set:

- HSTS (`stsSeconds=31536000`, includeSubdomains, preload)
- `X-Content-Type-Options`, XSS filter, `Referrer-Policy`, `X-Frame-Options=DENY`
- A restrictive `Content-Security-Policy` suitable for the self-hosted UI
- Rate limit average 100 / burst 200 (tune for your edge)

Adjust middleware names or CSP if you front additional assets. Keep
`TRUSTED_PROXIES` narrow; an overly broad CIDR defeats client-IP gating for
the Owner's Emergency Password. `BREAK_GLASS_ALLOWED_CIDRS` lists the LAN
networks that accept it: an empty value is loopback-only, with no implicit
allow-all mode.

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
