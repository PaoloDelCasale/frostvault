from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import (
    FastAPI,
    Request,
)
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from .api_models import documented_schemas
from .branding import PRODUCT_NAME
from .config import settings, validate_settings
from .errors import STATUS, DomainError
from .i18n import (
    translate,
)
from .database import close_pools, db, initialize_database
from .services.source_layout import (
    prepare_sources_layout,
    reconcile_source_volume_identities,
    validate_nested_mounts_after_identity,
)
from .services import vault_relocation as vault_relocation_service
from .services import problem_families
from .services import protection_stop as protection_stop_service
from .services import throughput as throughput_service
from .services.shutdown import shutdown_state
from .sessions import (
    csrf_token_for,
)
from .storage import (
    background_loop,
    cleanup_abandoned_restore_files,
    start_restore_residue_sweep,
    filesystem_watch_loop,
    auto_upload_loop,
    cloud_preflight_loop,
    upload_remediation_loop,
    bulk_upload_admission_loop,
    history_retention_loop,
    drain_background_executors,
    stop_background_executors,
    reconcile_interrupted_jobs,
)
from .routes import browse, jobs, ops, policy, register_routes, sign_in, storage_plan, vaults
from .routes.deps import _read_session_cookie, _request_locale

# The inbox learns here how each family of Problems closes (#804).
problem_families.install()


@asynccontextmanager
async def lifespan(_: FastAPI):
    shutdown_state.reset()
    validate_settings()
    if os.geteuid() == 0:
        # #679: PUID=0 is an explicit choice; the entrypoint already checked
        # that root holds no capability that bypasses file permissions, or
        # only DAC_OVERRIDE/DAC_READ_SEARCH by explicit opt-in (#681).
        from .runtime_privileges import read_status, startup_warnings
        for warning in startup_warnings(read_status(), euid=os.geteuid(), gid=os.getegid(),
                                        groups=os.getgroups()):
            print(warning, flush=True)
    prepare_sources_layout()
    # Schema guard (ADR-0035, ADR-0041): initialize an empty database, upgrade
    # one of an earlier revision, refuse any other, before any write.
    initialize_database()
    # Identity must be reconciled before scans, workers, or watchers start.
    reconcile_source_volume_identities()
    validate_nested_mounts_after_identity()
    # Legacy healthy roots are enrolled before any watcher or Job can touch
    # them. A root already missing at upgrade stays ambiguous and cannot be
    # rebound by the relocation workflow.
    vault_relocation_service.reconcile_vault_root_identities()
    # A v1/v2 root identity becomes v3 when it is the same folder: on FUSE the
    # same path of the same Source Volume (#787, ADR-0055); elsewhere
    # unchanged, or changed only in its anonymous device number (#784, ADR-0054).
    vault_relocation_service.upgrade_legacy_root_identities()
    cleanup_abandoned_restore_files()
    reconcile_interrupted_jobs()
    protection_stop_service.reconcile_all()
    # The walk of every Vault tree runs beside the server, not before it
    # answers (#777); Recover Jobs wait for it.
    start_restore_residue_sweep()
    tasks = [asyncio.create_task(background_loop()), asyncio.create_task(auto_upload_loop()),
             asyncio.create_task(cloud_preflight_loop()), asyncio.create_task(upload_remediation_loop()),
             asyncio.create_task(bulk_upload_admission_loop()),
             asyncio.create_task(history_retention_loop()),
             # In-memory speed samples (#755); informational, lost at restart.
             asyncio.create_task(throughput_service.sampler_loop(db))]
    if settings.filesystem_watch_enabled:
        tasks.append(asyncio.create_task(filesystem_watch_loop()))
    shutdown_state.register_producers(tasks)
    shutdown_state.register_stop_callback(stop_background_executors)
    try:
        yield
    finally:
        # Also works for non-uvicorn ASGI/test lifespans. In production the
        # server has already signalled SSE and stopped these producers BEFORE
        # waiting for HTTP connections, rather than only reaching this point.
        shutdown_state.request()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not await drain_background_executors():
            raise RuntimeError("Background workers exceeded the shutdown drain budget")
        # Workers are drained: close the idle pooled connections (#535).
        close_pools()


app = FastAPI(
    title=PRODUCT_NAME,
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)


def _openapi_schema() -> dict[str, Any]:
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, documented in documented_schemas().items():
            existing = components.get(name)
            # Never replace a canonical inbound Pydantic contract. Named
            # pass-through response components are identifiable by their open
            # object schema and are enriched with the documented field shape.
            if existing is None or existing.get("additionalProperties") is True:
                components[name] = documented
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi_schema


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Login has no session yet, so it cannot carry a synchronizer token; it is
# protected by the strict Origin check plus break-glass network gating.
CSRF_EXEMPT_PATHS = {"/api/login"}


def _allowed_hosts() -> list[str]:
    return [host.strip() for host in settings.allowed_hosts.split(",") if host.strip()]


def _origin_allowed(origin: str) -> bool:
    parsed = urlsplit(origin)
    if not parsed.hostname:
        return False
    if parsed.hostname not in _allowed_hosts():
        return False
    if settings.cookie_secure and parsed.scheme != "https":
        return False
    return True


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    allowed_hosts = _allowed_hosts()
    if allowed_hosts:
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and allowed_hosts and not _origin_allowed(origin):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if request.url.path not in CSRF_EXEMPT_PATHS:
            token = _read_session_cookie(request)
            if token:
                with db() as connection:
                    expected = csrf_token_for(connection, token)
                if expected is not None:
                    header = request.headers.get("x-csrf-token") or ""
                    if not secrets.compare_digest(header, expected):
                        return JSONResponse(
                            {"detail": "CSRF token missing or invalid"},
                            status_code=403,
                        )
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        # Authenticated state and catalog listings are network-only, including
        # errors: neither the browser nor an intermediary may retain them.
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(DomainError)
async def _domain_error_handler(request: Request, error: DomainError) -> JSONResponse:
    """Every refusal of the API: one body, and the status its ``code`` stands for.

    ``code`` is the stable kind of refusal (the frontend keys on
    ``reauth_required`` to trigger a step-up); ``detail`` is the message in the
    request's language.
    """
    return JSONResponse(
        {
            "detail": translate(
                error.message_key, locale=_request_locale(request), **error.message_params),
            "code": error.code,
            "message_key": error.message_key,
            "message_params": error.message_params,
        },
        status_code=STATUS[error.code],
        headers=error.headers,
    )


# Routes in the historical registration order, flat on the application: the
# OpenAPI paths keep their order and the SPA fallback of ``ops.router`` stays last.
register_routes(
    app,
    sign_in.router,
    ops.pages_router,
    sign_in.session_router,
    vaults.router,
    browse.router,
    jobs.router,
    vaults.inventory_router,
    policy.router,
    storage_plan.router,
    ops.router,
)
