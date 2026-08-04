"""Non-filtering response contracts used by the HTTP API.

The existing JSON payloads are the compatibility boundary.  Every generated
response model therefore validates that a JSON object was returned while
preserving all keys, including keys added by service integrations.  The richer
field schemas in ``openapi_schemas.json`` document the canonical shapes without
turning this annotation-only migration into an outbound serializer migration.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import RootModel


class JsonObjectResponse(RootModel[dict[str, Any]]):
    """A JSON object response that never filters nested or extension fields."""


@lru_cache(maxsize=None)
def response_model(name: str) -> type[JsonObjectResponse]:
    """Return a named pass-through response model for an OpenAPI component."""

    return type(name, (JsonObjectResponse,), {"__module__": __name__})


def documented_schemas() -> dict[str, Any]:
    """Load deterministic component schemas shared with generated clients."""

    path = Path(__file__).with_name("openapi_schemas.json")
    return json.loads(path.read_text(encoding="utf-8"))["schemas"]
