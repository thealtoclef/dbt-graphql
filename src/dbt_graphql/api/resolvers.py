from __future__ import annotations

from typing import Any

from ariadne import QueryType
from graphql import GraphQLError

from ..cache.compiled_plan import compile_with_cache
from ..cache.result import execute_with_cache
from ..compiler.query import compile_query
from ..config import CacheConfig
from .policy import PolicyError

from loguru import logger


def create_query_type(registry) -> QueryType:
    """Build the GraphQL ``Query`` resolver set.

    Cache config is threaded to resolvers via ``info.context["cache_config"]``
    (set by ``create_app``); resolvers handle ``None`` as "caching disabled".
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
        cache_cfg: CacheConfig | None = ctx.get("cache_config")
        jwt_payload = ctx.get("jwt_payload")
        policy_engine = ctx.get("policy_engine")

        # Build a resolve_policy callback (used both directly and inside
        # compile_query for nested-table policy resolution).
        resolve_policy = None
        if policy_engine is not None:
            resolve_policy = lambda t: policy_engine.evaluate(t, jwt_payload)  # noqa: E731

        # Per-call zero-arg compiler thunk. Kept here (not in the cache
        # module) so the cache layer never has to know compile_query's
        # signature.
        def _compile():
            return compile_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=registry,
                dialect=dialect,
                limit=kwargs.get("limit"),
                offset=kwargs.get("offset"),
                where=kwargs.get("where"),
                resolve_policy=resolve_policy,
            )

        try:
            if cache_cfg is not None and cache_cfg.compiled_plan.enabled:
                stmt = await compile_with_cache(
                    field_node=info.field_nodes[0],
                    table_name=table_name,
                    where=kwargs.get("where"),
                    limit=kwargs.get("limit"),
                    offset=kwargs.get("offset"),
                    dialect=dialect,
                    jwt_payload=jwt_payload,
                    compiler=_compile,
                )
            else:
                stmt = _compile()
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc

        logger.debug("query {}: {}", table_name, stmt)

        if cache_cfg is not None and cache_cfg.result.enabled:
            rows = await execute_with_cache(
                stmt,
                dialect_name=dialect,
                table_names=(table_name,),
                runner=db.execute,
                cfg=cache_cfg.result,
            )
        else:
            rows = await db.execute(stmt)

        logger.debug("query {} returned {} rows", table_name, len(rows))
        return rows

    return resolve_table


def _to_graphql_error(exc: PolicyError) -> GraphQLError:
    """Translate a PolicyError into a structured GraphQL error.

    Clients get a stable ``code`` plus ``table`` / ``columns`` in
    ``extensions`` so they can programmatically detect denials.
    """
    extensions: dict[str, Any] = {"code": exc.code}
    table = getattr(exc, "table", None)
    if table is not None:
        extensions["table"] = table
    columns = getattr(exc, "columns", None)
    if columns is not None:
        extensions["columns"] = columns
    return GraphQLError(str(exc), extensions=extensions)
