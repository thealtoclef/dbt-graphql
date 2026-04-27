"""Granian runners — one entry point per transport."""

from __future__ import annotations

from contextlib import asynccontextmanager

from loguru import logger
from starlette.applications import Starlette
from starlette.routing import Mount

from ..config import AppConfig
from ..formatter.schema import TableRegistry
from ..graphql.app import create_app
from ..graphql.monitoring import instrument_starlette
from ..graphql.policy import AccessPolicy

_asgi_app: Starlette | None = None
_mcp_asgi_app: Starlette | None = None


def serve_graphql(
    *,
    registry: TableRegistry,
    config: AppConfig,
    access_policy: AccessPolicy | None = None,
    mcp_http_app=None,
) -> None:
    """Run the GraphQL app (and optionally co-mounted MCP) under Granian."""
    from granian import Granian
    from granian.constants import Interfaces
    from granian.log import LogLevels

    if config.serve is None:
        raise ValueError("config.serve is required to run the serve layer")

    global _asgi_app
    _asgi_app = create_app(
        registry=registry,
        config=config.db,
        access_policy=access_policy,
        cache_config=config.cache,
        jwt_config=config.security.jwt,
        mcp_http_app=mcp_http_app,
    )
    host = config.serve.host
    port = config.serve.port
    log_level = LogLevels(config.monitoring.logs.level.lower())
    logger.info("listening on http://{}:{}", host, port)
    Granian(
        target=f"{__name__}:_asgi_app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        log_level=log_level,
    ).serve()


def serve_mcp(*, mcp_http_app, config: AppConfig | None = None) -> None:
    """Run MCP as a standalone HTTP app under Granian (no GraphQL endpoint)."""
    from granian import Granian
    from granian.constants import Interfaces
    from granian.log import LogLevels

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp_http_app.lifespan(app):
            logger.info("MCP server ready at /mcp")
            yield

    global _mcp_asgi_app
    _mcp_asgi_app = Starlette(
        lifespan=lifespan,
        routes=[Mount("/mcp", mcp_http_app)],
    )
    instrument_starlette(_mcp_asgi_app)

    host = config.serve.host if config and config.serve else "0.0.0.0"
    port = config.serve.port if config and config.serve else 8000
    log_level = (
        LogLevels(config.monitoring.logs.level.lower()) if config else LogLevels.info
    )

    logger.info("MCP server listening on http://{}:{}/mcp", host, port)
    Granian(
        target=f"{__name__}:_mcp_asgi_app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        log_level=log_level,
    ).serve()
