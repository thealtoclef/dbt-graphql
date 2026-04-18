"""FastAPI + Ariadne app factory for the GraphQL-to-SQL engine.

Usage::

    from dbt_mdl.graphql import create_app, serve

    app = create_app(db_graphql_path="db.graphql", db_url="mysql+aiomysql://...")
    # or with config dict:
    app = create_app(db_graphql_path="db.graphql", config={"type": "mysql", ...})

    serve(app, host="0.0.0.0", port=8080)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from fastapi import FastAPI

from .connection import DatabaseManager
from .resolvers import create_query_type
from .schema import load_db_graphql


def create_app(
    *,
    db_graphql_path: str | Path,
    db_url: str | None = None,
    config: dict[str, Any] | None = None,
) -> FastAPI:
    """Build a FastAPI app with Ariadne GraphQL mounted at ``/graphql``.

    Parameters
    ----------
    db_graphql_path:
        Path to the ``db.graphql`` SDL file.
    db_url:
        SQLAlchemy async connection URL (e.g. ``mysql+aiomysql://...``).
    config:
        Alternative: a dict with ``type``, ``host``, ``port``, etc.
        Passed to ``DatabaseManager`` which builds the URL.
    """
    schema_info, registry = load_db_graphql(db_graphql_path)
    db = DatabaseManager(db_url=db_url, config=config)

    query_type = create_query_type(registry)

    sdl = Path(db_graphql_path).read_text()
    gql_schema = make_executable_schema(sdl, query_type)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        yield
        await db.close()

    app = FastAPI(lifespan=lifespan)

    graphql_app = GraphQL(
        gql_schema,
        context_value=lambda req: {
            "request": req,
            "registry": registry,
            "db": db,
        },
    )
    app.mount("/graphql", graphql_app)

    return app


_asgi_app: FastAPI | None = None


def serve(
    *,
    db_graphql_path: str | Path,
    db_url: str | None = None,
    config: dict[str, Any] | None = None,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> None:
    """Create and run the app with granian."""
    from granian import Granian
    from granian.constants import Interfaces

    global _asgi_app
    _asgi_app = create_app(
        db_graphql_path=db_graphql_path,
        db_url=db_url,
        config=config,
    )
    Granian(
        target=f"{__name__}:_asgi_app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
    ).serve()
