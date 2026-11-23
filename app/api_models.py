"""Non-filtering response contracts used by the HTTP API.

The existing JSON payloads are the compatibility boundary.  A route's response
is declared through a pass-through: at runtime it only checks that a JSON
object was returned and preserves all keys, including keys added by service
integrations.  What the document says about the fields comes from one of two
places while the schemas move (ADR-0056): a Pydantic model next to the route,
declared with ``passthrough(Model)``, or the hand-written schema of the same
name in ``openapi_schemas.json``, declared with ``response_model(name)``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, RootModel


class JsonObjectResponse(RootModel[dict[str, Any]]):
    """A JSON object response that never filters nested or extension fields."""


@lru_cache(maxsize=None)
def passthrough(model: type[BaseModel]) -> type[JsonObjectResponse]:
    """Declare ``model`` as a response without validating or filtering with it.

    The returned class validates and serializes exactly like
    ``JsonObjectResponse``; only its JSON Schema is the model's, so the OpenAPI
    document references ``model`` itself.  The tests' HTTP client reads
    ``__contract__`` and checks every JSON success body against it.
    """

    def model_json_schema(cls, _core_schema: Any, handler: Any) -> Any:
        return handler(model.__pydantic_core_schema__)

    return type(
        model.__name__,
        (JsonObjectResponse,),
        {
            "__module__": __name__,
            "__contract__": model,
            "__get_pydantic_json_schema__": classmethod(model_json_schema),
        },
    )


@lru_cache(maxsize=None)
def response_model(name: str) -> type[JsonObjectResponse]:
    """Return a named pass-through response model for an OpenAPI component."""

    return type(name, (JsonObjectResponse,), {"__module__": __name__})


def documented_schemas() -> dict[str, Any]:
    """Load deterministic component schemas shared with generated clients."""

    path = Path(__file__).with_name("openapi_schemas.json")
    return json.loads(path.read_text(encoding="utf-8"))["schemas"]
