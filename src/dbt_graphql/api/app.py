from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Mount

from ..cache import CacheConfig, close_cache, setup_cache
from ..cache.parsed_doc import configure_sync_lru, parse_sync_cached
from ..compiler.connection import DatabaseManager
from ..config import AppConfig, DbConfig
from ..formatter.schema import TableRegistry, load_db_graphql
from .monitoring import (
    build_graphql_http_handler,
    instrument_sqlalchemy,
    instrument_starlette,
)
from .policy import AccessPolicy, PolicyEngine
from .resolvers import create_query_type
from .security import JWTAuthBackend

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
    db_graphql_path: str | Path,
    db_url: str | None = None,
    config: DbConfig | None = None,
    access_policy: AccessPolicy | None = None,
    cache_config: CacheConfig | None = None,
) -> Starlette:
    """Build a Starlette app with Ariadne GraphQL mounted at ``/graphql``.

    ``cache_config=None`` means no caching at all — every request flows
    straight through parse/compile/execute. Useful for tests that want to
    measure the uncached baseline. Pass an explicit ``CacheConfig()`` to
    opt into the default 3-layer cache.
    """
    _, registry = load_db_graphql(db_graphql_path)
    db = DatabaseManager(db_url=db_url, config=config)
    policy_engine = PolicyEngine(access_policy) if access_policy is not None else None

    query_type = create_query_type(registry)

    # L1 hook: only override Ariadne's parser when L1 is enabled. Ariadne's
    # ``query_parser`` is sync — ``(context, data: dict) -> DocumentNode`` —
    # so we use the in-process LRU (cashews would require ``await``). The
    # plan §3.2 calls out that L1 is intentionally always in-memory.
    schema_kwargs: dict = {}
    if cache_config is not None and cache_config.parsed_doc.enabled:
        configure_sync_lru(cache_config.parsed_doc.max_size)

        def _parser(_ctx, data):
            return parse_sync_cached(data["query"])

        schema_kwargs["query_parser"] = _parser

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
        **schema_kwargs,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        logger.info("connecting to database")
        await db.connect()
        instrument_sqlalchemy(db._engine)
        if cache_config is not None:
            setup_cache(cache_config)
        logger.info("app ready — serving GraphQL at /graphql")
        yield
        if cache_config is not None:
            await close_cache()
        await db.close()
        logger.info("database connection closed")

    app = Starlette(
        lifespan=lifespan,
        routes=[Mount("/graphql", graphql_app)],
        middleware=[Middleware(AuthenticationMiddleware, backend=JWTAuthBackend())],
    )
    instrument_starlette(app)
    return app


_asgi_app: Starlette | None = None


def serve(
    *,
    db_graphql_path: str | Path,
    config: AppConfig,
    access_policy: AccessPolicy | None = None,
) -> None:
    """Create and run the app with granian."""
    from granian import Granian
    from granian.constants import Interfaces
    from granian.log import LogLevels

    if config.serve is None:
        raise ValueError("config.serve is required to run the API server")

    global _asgi_app
    _asgi_app = create_app(
        db_graphql_path=db_graphql_path,
        config=config.db,
        access_policy=access_policy,
        cache_config=config.cache,
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
