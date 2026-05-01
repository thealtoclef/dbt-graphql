"""Single uvicorn entry point for the unified ASGI app."""

from __future__ import annotations

from loguru import logger
from starlette.applications import Starlette

from ..config import AppConfig
from ..schema.models import TableRegistry
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
    """Build the ASGI app and run it under uvicorn.

    GraphQL always mounts at ``/graphql``. MCP additionally mounts at
    ``/mcp`` when ``config.serve.mcp_enabled`` is true.
    """
    import uvicorn

    if config.serve is None:
        raise ValueError("config.serve is required to run the serve layer")

    mcp_factory = None
    if config.serve.mcp_enabled:
        from ..mcp.server import build_mcp_factory

        mcp_factory = build_mcp_factory(project)

    global _asgi_app
    _asgi_app = create_app(
        registry=registry,
        config=config.db,
        access_policy=access_policy,
        cache_config=config.cache,
        graphql_config=config.graphql,
        jwt_config=config.security.jwt,
        security_enabled=not config.dev_mode,
        mcp_factory=mcp_factory,
    )

    host = config.serve.host
    port = config.serve.port
    log_level = config.monitoring.logs.level.lower()

    endpoints = "/graphql" + (" + /mcp" if config.serve.mcp_enabled else "")
    logger.info("listening on http://{}:{} — serving {}", host, port, endpoints)

    uvicorn.run(
        app=f"{__name__}:_asgi_app",
        host=host,
        port=port,
        log_level=log_level,
    )
