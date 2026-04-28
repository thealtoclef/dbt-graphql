"""Top-level ASGI app composition.

Owns Starlette assembly, lifespan ordering (DB pool + cache + co-mounted
MCP), auth middleware, and OTel instrumentation. The GraphQL and MCP
sub-apps are built by their own modules and mounted here — composition
is *the* concern of this module.

GraphQL is always mounted at ``/graphql``. MCP is opt-in via
``mcp_http_app`` and mounts at ``/mcp`` when supplied.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

from loguru import logger
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Mount

from ..cache import CacheConfig, close_cache, setup_cache
from ..compiler.connection import DatabaseManager
from ..config import DbConfig, GraphQLConfig, JWTConfig, PoolConfig
from ..formatter.schema import TableRegistry
from ..graphql.app import GraphQLBundle, create_graphql_subapp
from ..graphql.auth import auth_on_error, build_auth_backend
from ..graphql.monitoring import instrument_sqlalchemy, instrument_starlette
from ..graphql.policy import AccessPolicy


def create_app(
    *,
    registry: TableRegistry,
    db_url: str | None = None,
    config: DbConfig | None = None,
    access_policy: AccessPolicy | None = None,
    cache_config: CacheConfig | None = None,
    graphql_config: GraphQLConfig | None = None,
    jwt_config: JWTConfig,
    security_enabled: bool = False,
    introspection: bool = False,
    pool_config: PoolConfig | None = None,
    mcp_factory=None,
) -> Starlette:
    """Build the unified Starlette app behind one auth middleware and one
    lifespan.

    GraphQL always mounts at ``/graphql``. MCP mounts at ``/mcp`` when
    ``mcp_factory`` is provided — a callable taking the GraphQL bundle
    and returning a Starlette/ASGI sub-app. Passing the bundle into the
    factory lets the MCP layer reuse the same executable schema, the
    same per-request context-builder, the same DB pool, and the same
    ``PolicyEngine`` — so policy enforcement is structurally shared.

    ``cache_config=None`` disables the result cache entirely. Pass
    ``CacheConfig()`` to opt into the default result cache + singleflight.
    """
    db = DatabaseManager(db_url=db_url, config=config, pool=pool_config)
    bundle: GraphQLBundle = create_graphql_subapp(
        registry=registry,
        db=db,
        access_policy=access_policy,
        cache_config=cache_config,
        graphql_config=graphql_config,
        introspection=introspection,
    )
    mcp_http_app = mcp_factory(bundle) if mcp_factory is not None else None
    auth_backend, owned_http = build_auth_backend(jwt_config, enabled=security_enabled)

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

    routes = [Mount("/graphql", bundle.asgi)]
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
