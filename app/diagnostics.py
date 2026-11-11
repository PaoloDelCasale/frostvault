"""Independent, deployment-owned authentication for Prometheus scrapers."""
from __future__ import annotations

import re
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_metrics_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="MetricsBearer",
    description="Independent METRICS_TOKEN scraper credential; never an Owner Session",
)
# RFC 6750: case-insensitive scheme, one or more spaces and a b64token.
# Parse the raw header: HTTPBearer normalizes credential whitespace.
_BEARER_HEADER = re.compile(r"(?i:Bearer) +([A-Za-z0-9._~+/-]+=*)\Z")


def require_metrics_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_metrics_bearer),
) -> None:
    expected = settings.metrics_token
    if not expected.strip():
        raise HTTPException(404, "Not Found", headers={"Cache-Control": "no-store"})
    headers = request.headers.getlist("authorization")
    matched = _BEARER_HEADER.fullmatch(headers[0]) if len(headers) == 1 else None
    if (
        credentials is None
        or matched is None
        or not secrets.compare_digest(
            matched[1].encode("utf-8"), expected.encode("utf-8")
        )
    ):
        # Never echo supplied/expected credentials or query metric state here.
        raise HTTPException(
            401,
            "Invalid metrics credential",
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
