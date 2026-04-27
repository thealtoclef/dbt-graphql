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
    registry: TableRegistry,
    config: AppConfig,
    project,
    access_policy: AccessPolicy | None = None,
) -> None:
    """Build the ASGI app and run it under Granian.

    GraphQL always mounts at ``/graphql``. MCP additionally mounts at
    ``/mcp`` when ``config.serve.mcp_enabled`` is true.
    """
    from granian import Granian
    from granian.constants import Interfaces
    from granian.log import LogLevels

    if config.serve is None:
        raise ValueError("config.serve is required to run the serve layer")

    mcp_factory = None
    if config.serve.mcp_enabled:
        from ..mcp.server import build_mcp_factory

        mcp_factory = build_mcp_factory(project, enrichment=config.enrichment)

    global _asgi_app
    _asgi_app = create_app(
        registry=registry,
        config=config.db,
        access_policy=access_policy,
        cache_config=config.cache,
        jwt_config=config.security.jwt,
        introspection=config.serve.graphql_introspection,
        mcp_factory=mcp_factory,
    )

    host = config.serve.host
    port = config.serve.port
    log_level = LogLevels(config.monitoring.logs.level.lower())

    endpoints = "/graphql" + (" + /mcp" if config.serve.mcp_enabled else "")
    logger.info("listening on http://{}:{} — serving {}", host, port, endpoints)

    Granian(
        target=f"{__name__}:_asgi_app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        log_level=log_level,
    ).serve()
