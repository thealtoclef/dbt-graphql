from __future__ import annotations

import functools
from typing import Any

from ariadne import QueryType
from graphql import GraphQLError
from loguru import logger
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

from ..cache.result import execute_with_cache
from ..compiler.query import compile_query
from ..config import CacheConfig
from .policy import PolicyError

# GraphQL extension code paired with the HTTP handler's 503 elevation.
POOL_TIMEOUT_CODE = "POOL_TIMEOUT"


def create_query_type(registry) -> QueryType:
    """Build the GraphQL ``Query`` resolver set.

    Cache config is threaded to resolvers via ``info.context["cache_config"]``
    — always present; the cache cannot be disabled.
    """
    query_type = QueryType()
    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_resolver(name))
    return query_type


def _make_resolver(table_name: str):

    async def resolve_table(_, info, **kwargs) -> list[dict[str, Any]]:
        ctx = info.context
        registry = ctx["registry"]
        tdef = registry.get(table_name)
        if tdef is None:
            raise ValueError(f"Unknown table: {table_name}")

        db = ctx["db"]
        dialect = db.dialect_name
        cache_cfg: CacheConfig = ctx["cache_config"]
        jwt_payload = ctx.get("jwt_payload")
        policy_engine = ctx.get("policy_engine")

        resolve_policy = None
        if policy_engine is not None:
            resolve_policy = functools.partial(policy_engine.evaluate, ctx=jwt_payload)

        try:
            stmt = compile_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=registry,
                dialect=dialect,
                limit=kwargs.get("limit"),
                offset=kwargs.get("offset"),
                where=kwargs.get("where"),
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc

        logger.debug("query {}: {}", table_name, stmt)

        try:
            rows = await execute_with_cache(
                stmt,
                dialect_name=dialect,
                runner=db.execute,
                cfg=cache_cfg,
            )
        except SAPoolTimeoutError as exc:
            raise GraphQLError(
                "database connection pool exhausted",
                extensions={
                    "code": POOL_TIMEOUT_CODE,
                    "retry_after": db._pool.retry_after,
                },
            ) from exc

        logger.debug("query {} returned {} rows", table_name, len(rows))
        return rows

    return resolve_table


def _to_graphql_error(exc: PolicyError) -> GraphQLError:
    """Translate a PolicyError into a structured GraphQL error.

    Clients get a stable ``code`` plus ``table`` / ``columns`` in
    ``extensions`` so they can programmatically detect denials.
    """
    extensions: dict[str, Any] = {"code": exc.code}
    for attr in ("table", "column", "columns"):
        value = getattr(exc, attr, None)
        if value is not None:
            extensions[attr] = value
    return GraphQLError(str(exc), extensions=extensions)
