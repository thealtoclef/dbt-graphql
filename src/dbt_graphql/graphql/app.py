from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Mount

from ..cache import CacheConfig, close_cache, setup_cache
from ..compiler.connection import DatabaseManager
from ..config import DbConfig, JWTConfig
from ..formatter.schema import TableRegistry
from .auth import auth_on_error, build_auth_backend
from .monitoring import (
    build_graphql_http_handler,
    instrument_sqlalchemy,
    instrument_starlette,
)
from .policy import AccessPolicy, PolicyEngine
from .resolvers import create_query_type

from loguru import logger

_STANDARD_GQL_SCALARS = {"String", "Int", "Float", "Boolean", "ID"}


def _build_ariadne_sdl(registry: TableRegistry) -> str:
    """Build a standard GraphQL SDL (without db.graphql custom directives) for Ariadne.

    The db.graphql format uses custom directives (@table, @column, @relation, etc.)
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
    registry: TableRegistry,
    db_url: str | None = None,
    config: DbConfig | None = None,
    access_policy: AccessPolicy | None = None,
    cache_config: CacheConfig | None = None,
    jwt_config: JWTConfig,
    mcp_http_app=None,
) -> Starlette:
    """Build a Starlette app with GraphQL at ``/graphql`` and optionally MCP at ``/mcp``.

    ``cache_config=None`` disables the result cache entirely — every
    request goes through to the warehouse. Pass an explicit
    ``CacheConfig()`` to opt into the default result cache + singleflight.
    """
    db = DatabaseManager(db_url=db_url, config=config)
    policy_engine = PolicyEngine(access_policy) if access_policy is not None else None

    query_type = create_query_type(registry)
    gql_schema = make_executable_schema(_build_ariadne_sdl(registry), query_type)

    graphql_app = GraphQL(
        gql_schema,
        context_value=lambda req, _data=None: {
            "request": req,
            "registry": registry,
            "db": db,
            "jwt_payload": req.user.payload,
            "policy_engine": policy_engine,
            "cache_config": cache_config,
        },
        http_handler=build_graphql_http_handler(),
    )

    auth_backend, owned_http = build_auth_backend(jwt_config)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with contextlib.AsyncExitStack() as stack:
            if mcp_http_app is not None:
                await stack.enter_async_context(mcp_http_app.lifespan(_app))
            logger.info("connecting to database")
            await db.connect()
            instrument_sqlalchemy(db._engine)
            if cache_config is not None:
                setup_cache(cache_config)
            endpoints = "/graphql" + (" + /mcp" if mcp_http_app is not None else "")
            logger.info("app ready — serving {}", endpoints)
            yield
            if cache_config is not None:
                await close_cache()
            if owned_http is not None:
                await owned_http.aclose()
            await db.close()
            logger.info("database connection closed")

    routes = [Mount("/graphql", graphql_app)]
    if mcp_http_app is not None:
        routes.append(Mount("/mcp", mcp_http_app))

    app = Starlette(
        lifespan=lifespan,
        routes=routes,
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=auth_backend,
                on_error=auth_on_error,
            )
        ],
    )
    instrument_starlette(app)
    return app
