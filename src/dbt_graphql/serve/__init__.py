"""Single Granian entry point for the unified ASGI app."""

from __future__ import annotations

from loguru import logger
from starlette.applications import Starlette

from ..config import AppConfig
from ..formatter.schema import TableRegistry
from ..graphql.policy import AccessPolicy
from .app import create_app

_asgi_app: Starlette | None = None


def run(
    *,
    registry: TableRegistry | None,
    config: AppConfig,
    project,
    access_policy: AccessPolicy | None = None,
) -> None:
    """Build the ASGI app and run it under Granian.

    Reads ``config.serve.graphql.enabled`` and ``config.serve.mcp.enabled``
    to decide which transports to mount. The CLI is responsible for
    validating that at least one is enabled before calling this.
    """
    from granian import Granian
    from granian.constants import Interfaces
    from granian.log import LogLevels

    if config.serve is None:
        raise ValueError("config.serve is required to run the serve layer")

    mcp_http_app = None
    if config.serve.mcp.enabled:
        from ..compiler.connection import DatabaseManager
        from ..mcp.server import create_mcp_http_app

        mcp_db = DatabaseManager(config=config.db)
        mcp_http_app = create_mcp_http_app(
            project, db=mcp_db, enrichment=config.enrichment
        )

    global _asgi_app
    _asgi_app = create_app(
        registry=registry,
        config=config.db,
        access_policy=access_policy,
        cache_config=config.cache,
        jwt_config=config.security.jwt,
        mcp_http_app=mcp_http_app,
        introspection=config.serve.graphql.introspection,
        graphql_enabled=config.serve.graphql.enabled,
    )

    host = config.serve.host
    port = config.serve.port
    log_level = LogLevels(config.monitoring.logs.level.lower())

    endpoints = " + ".join(
        p for p in (
            "/graphql" if config.serve.graphql.enabled else "",
            "/mcp" if config.serve.mcp.enabled else "",
        ) if p
    )
    logger.info("listening on http://{}:{} — serving {}", host, port, endpoints)

    Granian(
        target=f"{__name__}:_asgi_app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        log_level=log_level,
    ).serve()
