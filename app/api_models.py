"""Non-filtering response contracts used by the HTTP API.

The existing JSON payloads are the compatibility boundary.  A route's response
is declared through a pass-through: at runtime it only checks that a JSON
object was returned and preserves all keys, including keys added by service
integrations.  What the document says about the fields comes from a Pydantic
model next to the route, declared with ``passthrough(Model)`` (ADR-0056).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel


class JsonObjectResponse(RootModel[dict[str, Any]]):
    """A JSON object response that never filters nested or extension fields."""


class ResponseModel(BaseModel):
    """The base of every model that documents a response, and of the models it nests.

    Production never validates with these models: they only describe the
    OpenAPI document and are checked by the tests. ``defer_build`` keeps
    Pydantic from building them at import; they are built the first time the
    document is generated or a test validates an answer.
    """

    model_config = ConfigDict(defer_build=True)


@lru_cache(maxsize=None)
def passthrough(model: type[BaseModel]) -> type[JsonObjectResponse]:
    """Declare ``model`` as a response without validating or filtering with it.

    The returned class validates and serializes exactly like
    ``JsonObjectResponse``; only its JSON Schema is the model's, so the OpenAPI
    document references ``model`` itself.  The tests' HTTP client reads
    ``__contract__`` and checks every JSON success body against it.
    """

    def model_json_schema(cls, _core_schema: Any, handler: Any) -> Any:
        # A ``ResponseModel`` is built here, the first time the document is
        # generated; already built, this does nothing.
        model.model_rebuild()
        schema = model.__pydantic_core_schema__
        if schema["type"] == "definitions":
            # A model that nests another one twice keeps its own reference here:
            # left in, the document would hold two components of the same name.
            inner = {key: value for key, value in schema["schema"].items() if key != "ref"}
            schema = {**schema, "schema": inner}
        return handler(schema)

    return type(
        model.__name__,
        (JsonObjectResponse,),
        {
            "__module__": __name__,
            "__contract__": model,
            "__get_pydantic_json_schema__": classmethod(model_json_schema),
        },
    )
