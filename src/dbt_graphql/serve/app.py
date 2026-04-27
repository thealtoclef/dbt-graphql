"""Top-level ASGI app composition.

Owns Starlette assembly, lifespan ordering (DB pool + cache + co-mounted
MCP), auth middleware, and OTel instrumentation. The GraphQL and MCP
sub-apps are built by their own modules and mounted here — composition
is *the* concern of this module.
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
from ..config import DbConfig, JWTConfig, PoolConfig
from ..formatter.schema import TableRegistry
from ..graphql.app import create_graphql_subapp
from ..graphql.auth import auth_on_error, build_auth_backend
from ..graphql.monitoring import instrument_sqlalchemy, instrument_starlette
from ..graphql.policy import AccessPolicy


def create_app(
    *,
    registry: TableRegistry | None = None,
    db_url: str | None = None,
    config: DbConfig | None = None,
    access_policy: AccessPolicy | None = None,
    cache_config: CacheConfig | None = None,
    jwt_config: JWTConfig,
    mcp_http_app=None,
    introspection: bool = False,
    graphql_enabled: bool = True,
    pool_config: PoolConfig | None = None,
) -> Starlette:
    """Build the unified Starlette app behind one auth middleware and one
    lifespan. GraphQL mounts at ``/graphql`` when ``graphql_enabled`` (the
    default); MCP mounts at ``/mcp`` when ``mcp_http_app`` is provided.

    At least one transport must be enabled or this raises ``ValueError``.

    ``cache_config=None`` disables the result cache entirely. Pass
    ``CacheConfig()`` to opt into the default result cache + singleflight.
    """
    if not graphql_enabled and mcp_http_app is None:
        raise ValueError(
            "create_app: at least one of graphql_enabled or mcp_http_app "
            "must be set, otherwise the app has no routes."
        )
    if graphql_enabled and registry is None:
        raise ValueError(
            "create_app: registry is required when graphql_enabled is True."
        )

    db: DatabaseManager | None = None
    graphql_app = None
    if graphql_enabled:
        assert registry is not None  # type narrowing; runtime-guarded above
        db = DatabaseManager(db_url=db_url, config=config, pool=pool_config)
        graphql_app = create_graphql_subapp(
            registry=registry,
            db=db,
            access_policy=access_policy,
            cache_config=cache_config,
            introspection=introspection,
        )
    auth_backend, owned_http = build_auth_backend(jwt_config)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with contextlib.AsyncExitStack() as stack:
            if mcp_http_app is not None:
                await stack.enter_async_context(mcp_http_app.lifespan(_app))
            if db is not None:
                logger.info("connecting to database")
                await db.connect()
                instrument_sqlalchemy(db._engine)
            if cache_config is not None:
                setup_cache(cache_config)
            endpoints = " + ".join(
                p for p in ("/graphql" if graphql_enabled else "",
                            "/mcp" if mcp_http_app is not None else "") if p
            )
            logger.info("app ready — serving {}", endpoints)
            yield
            if cache_config is not None:
                await close_cache()
            if owned_http is not None:
                await owned_http.aclose()
            if db is not None:
                await db.close()
                logger.info("database connection closed")

    routes = []
    if graphql_app is not None:
        routes.append(Mount("/graphql", graphql_app))
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
