"""FastAPI routers, one module per area; ``app.main`` registers their routes."""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, request_response


def register_routes(app: FastAPI, *routers: APIRouter) -> None:
    """Append the routes of ``routers``, in order, to the application's own table.

    ``include_router`` keeps an included router nested and resolves it again at
    every request (FastAPI 0.163); here the table stays flat, as if every route
    were declared on the application. A route built by a bare ``APIRouter()``
    differs from one built by the application only in the provider of
    ``dependency_overrides``, bound into its request handler: the handler is
    rebuilt with the application, the way ``APIRoute.__init__`` builds it.
    Defaults given to ``FastAPI(...)`` for every route (dependencies, response
    class, tags) would not reach these routes: the application sets none.
    """
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                raise TypeError(f"Only API routes are registered flat: {route!r}")
            route.dependency_overrides_provider = app
            route.app = request_response(route.get_route_handler())
            app.router.routes.append(route)
