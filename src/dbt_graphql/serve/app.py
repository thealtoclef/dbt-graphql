"""FastAPI + Ariadne app factory for the GraphQL-to-SQL engine.

Usage::

    from dbt_graphql.serve.app import create_app, serve

    app = create_app(db_graphql_path="db.graphql", db_url="mysql+aiomysql://...")
    # or with config dict:
    app = create_app(db_graphql_path="db.graphql", config={"type": "mysql", ...})
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from fastapi import FastAPI

from ..compiler.connection import DatabaseManager
from ..formatter.schema import TableRegistry, load_db_graphql
from .resolvers import create_query_type

_STANDARD_GQL_SCALARS = {"String", "Int", "Float", "Boolean", "ID"}


def _build_ariadne_sdl(registry: TableRegistry) -> str:
    """Build a standard GraphQL SDL (without db.graphql custom directives) for Ariadne.

    The db.graphql format uses custom directives (@sql, @database, @relation, etc.)
    that Ariadne's schema builder doesn't understand. This function builds a clean
    SDL with custom types declared as scalars, per-table WhereInput types, and a
    Query type for all tables.
    """
    custom_scalars: set[str] = set()
    type_blocks: list[str] = []
    where_input_defs: list[str] = []

    for table_def in registry:
        lines = [f"type {table_def.name} {{"]
        input_lines = [f"input {table_def.name}WhereInput {{"]
        for col in table_def.columns:
            type_name = col.gql_type
            if type_name and type_name not in _STANDARD_GQL_SCALARS:
                custom_scalars.add(type_name)
            wrapped = f"[{type_name}]" if col.is_array else type_name
            if col.not_null:
                wrapped += "!"
            lines.append(f"  {col.name}: {wrapped}")
            if not col.is_array:
                input_lines.append(f"  {col.name}: {type_name}")
        lines.append("}")
        input_lines.append("}")
        type_blocks.append("\n".join(lines))
        where_input_defs.append("\n".join(input_lines))

    query_fields = [
        f"  {t.name}(limit: Int, offset: Int, where: {t.name}WhereInput): [{t.name}]"
        for t in registry
    ]
    query_block = "type Query {\n" + "\n".join(query_fields) + "\n}"

    scalar_defs = [f"scalar {s}" for s in sorted(custom_scalars)]
    parts = scalar_defs + where_input_defs + type_blocks + [query_block]
    return "\n\n".join(parts) + "\n"


def create_app(
    *,
    db_graphql_path: str | Path,
    db_url: str | None = None,
    config: dict[str, Any] | None = None,
) -> FastAPI:
    """Build a FastAPI app with Ariadne GraphQL mounted at ``/graphql``."""
    schema_info, registry = load_db_graphql(db_graphql_path)
    db = DatabaseManager(db_url=db_url, config=config)

    query_type = create_query_type(registry)
    gql_schema = make_executable_schema(_build_ariadne_sdl(registry), query_type)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        yield
        await db.close()

    app = FastAPI(lifespan=lifespan)

    graphql_app = GraphQL(
        gql_schema,
        context_value=lambda req, _data=None: {
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
