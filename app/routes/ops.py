"""Operational probes, metrics and the SPA pages, assets and fallback."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ConfigDict
from starlette.routing import Match

from ..api_models import ResponseModel, passthrough
from ..config import settings
from ..diagnostics import require_metrics_token
from ..errors import DomainError, MethodNotAllowed, NotFound
from ..services import health as health_service
from ..services import metrics as metrics_service
from ..services.vault_paths import safe_local_path


router = APIRouter()

# Registered between the sign-in and the Session routes: two routers keep the
# historical registration order, and with it the order of the OpenAPI paths.
pages_router = APIRouter()


def _spa_dist_dir() -> Path:
    return Path(settings.frontend_dist_dir)


def _spa_index_response() -> FileResponse:
    index_path = _spa_dist_dir() / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "frontend/dist/index.html is missing. "
                "Build the SPA with: cd frontend && npm ci && npm run build"
            ),
        )
    return FileResponse(
        index_path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@pages_router.get("/vaults/new")
def vault_create_page():
    return _spa_index_response()


@pages_router.get("/")
def index():
    return _spa_index_response()


# The answers of the probes (ADR-0056). Both are closed: fixed keys, built here.

class HealthResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"]


class ReadinessChecks(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    database: bool
    worker: bool
    config: bool
    sources_layout: bool


class ReadinessResponse(ResponseModel):
    """The 200 answer; a 503 answers the same object with status not_ready."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


@router.get("/health", response_model=passthrough(HealthResponse))
def health():
    """Process liveness probe — does not inspect dependencies."""
    return {"status": "ok"}


@router.get("/ready", response_model=passthrough(ReadinessResponse))
def ready():
    """Dependency-aware readiness: database, worker heartbeat, and config."""
    report = health_service.readiness_report()
    status_code = 200 if report["status"] == "ready" else 503
    # Public probes expose dependency booleans, never workload/queue internals.
    return JSONResponse(
        {"status": report["status"], "checks": report["checks"]},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/metrics",
    response_class=Response,
    dependencies=[Depends(require_metrics_token)],
    responses={
        200: {
            "description": "Prometheus text exposition",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        401: {"description": "Missing or invalid independent Bearer credential"},
        404: {"description": "Metrics disabled (METRICS_TOKEN is empty)"},
    },
)
def metrics():
    """Prometheus exposition; disabled without METRICS_TOKEN, Bearer-only when set."""
    return Response(
        content=metrics_service.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


_SPA_FALLBACK_EXCLUDED_PREFIXES = frozenset({
    "api", "auth", "static", "assets",
    # Disabled diagnostics must remain 404, never the SPA or a built public file.
    "docs", "redoc", "openapi.json", "metrics",
    "manifest.webmanifest", "pwa-192.png", "pwa-512.png",
})


@router.get("/assets/{asset_path:path}")
def spa_asset(asset_path: str):
    """Serve hashed Vite build assets with long-lived immutable caching."""
    assets_root = (_spa_dist_dir() / "assets").resolve()
    if not assets_root.is_dir():
        raise HTTPException(status_code=404, detail="Not Found")
    # Sanitize before joining so user-controlled path segments never reach the
    # filesystem APIs unchecked (CodeQL path-injection / directory traversal).
    try:
        target = safe_local_path(str(assets_root), asset_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        target,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _spa_dist_file_response(full_path: str) -> FileResponse | None:
    """Serve a built public file from frontend/dist when it exists."""
    if not full_path or full_path.endswith("/"):
        return None
    dist_root = _spa_dist_dir().resolve()
    try:
        target = safe_local_path(str(dist_root), full_path)
    except ValueError:
        return None
    if not target.is_file() or target.name == "index.html":
        return None
    media_type, _ = mimetypes.guess_type(target.name)
    if target.suffix == ".js":
        media_type = "application/javascript; charset=utf-8"
    return FileResponse(
        target,
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _api_router_error(request: Request) -> DomainError:
    """The refusal of an address under /api/ that no route takes (#861).

    405 when routes of the application match its path for other methods (the
    router's own ``PARTIAL`` match, as Starlette answers), else 404. Only the
    catch-alls below call it, so routed requests pay nothing for it.
    """
    allow: set[str] = set()
    for route in request.app.router.routes:
        if getattr(route, "endpoint", None) in (spa_fallback, unknown_route):
            continue
        match, _ = route.matches(request.scope)
        if match is Match.PARTIAL:
            allow.update(route.methods or ())
    if allow:
        return MethodNotAllowed("api.method_not_allowed", allow=sorted(allow))
    return NotFound("api.not_found")


@router.get("/{full_path:path}")
def spa_fallback(full_path: str, request: Request):
    """SPA client-route fallback; never intercepts API, auth, or static assets."""
    if full_path.startswith("api/"):
        raise _api_router_error(request)
    head = full_path.split("/", 1)[0]
    if head in _SPA_FALLBACK_EXCLUDED_PREFIXES:
        raise HTTPException(status_code=404, detail="Not Found")
    built = _spa_dist_file_response(full_path)
    if built is not None:
        return built
    return _spa_index_response()


@router.api_route(
    "/{full_path:path}",
    methods=["POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def unknown_route(full_path: str, request: Request):
    """Unknown or removed routes are Not Found for every method.

    Without this the GET-only SPA fallback answers 405 to a mutation on a
    removed endpoint, which hides that the endpoint no longer exists. Under
    /api/ the answer is the API's own refusal, 405 for a known path.
    """
    if full_path.startswith("api/"):
        raise _api_router_error(request)
    raise HTTPException(status_code=404, detail="Not Found")
