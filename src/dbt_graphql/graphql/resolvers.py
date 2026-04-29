from __future__ import annotations

import asyncio
import functools
from typing import Any

from ariadne import ObjectType, QueryType
from graphql import GraphQLError
from loguru import logger
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

from ..cache.result import execute_with_cache
from ..compiler.query import (
    agg_fields_for_table,
    compile_aggregate_query,
    compile_group_query,
    compile_nodes_query,
)
from ..config import CacheConfig
from ..formatter.schema import TableDef
from ..formatter.sdl_view import effective_document, render_sdl
from .effective import effective_registry
from .policy import PolicyError

# GraphQL extension code paired with the HTTP handler's 503 elevation.
POOL_TIMEOUT_CODE = "POOL_TIMEOUT"

# Key used to stash the lazy-computed aggregate result on the carrier dict.
_AGG_FUTURE_KEY = "__agg_future__"


def create_query_type(registry) -> tuple[QueryType, list[ObjectType]]:
    """Build the GraphQL ``Query`` resolver set plus per-table ``ObjectType`` bindings.

    Returns a ``(QueryType, [ObjectType, ...])`` tuple. The caller must pass
    both to ``make_executable_schema`` so Ariadne can resolve sub-fields on
    ``{T}Result`` and ``{T}_group``.
    """
    query_type = QueryType()
    object_types: list[ObjectType] = []

    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_root_resolver(name))

        result_ot = ObjectType(f"{name}Result")
        result_ot.set_field("nodes", _make_nodes_resolver(name))
        result_ot.set_field("group", _make_group_resolver(name))

        all_agg = [fname for fname, _ in agg_fields_for_table(table_def)]
        for fname in all_agg:
            result_ot.set_field(fname, _make_aggregate_field_resolver(fname, table_def))

        object_types.append(result_ot)

    query_type.set_field("_sdl", _resolve_sdl)
    query_type.set_field("_tables", _resolve_tables)
    return query_type, object_types


def _resolve_sdl(_, info, tables: list[str] | None = None) -> str:
    """Return the effective db.graphql SDL for the current caller.

    Computed per-request from the caller's JWT and the active policy
    engine; never cached across users. When ``tables`` is provided the
    output is intersected with the caller's visible set — names not
    visible (denied or nonexistent) are silently skipped.
    """
    ctx = info.context
    eff = effective_registry(
        ctx["registry"], ctx.get("jwt_payload"), ctx.get("policy_engine")
    )
    restrict = set(tables) if tables is not None else None
    return render_sdl(effective_document(ctx["source_doc"], eff, restrict_to=restrict))


def _resolve_tables(_, info) -> list[dict]:
    """Summary info for tables visible to the current caller.

    Each entry is the index-page projection: ``name`` and ``description``.
    Structural detail (columns, relations) belongs to ``_sdl(tables: ...)`` —
    keep this view cheap so an agent can enumerate a 100-table warehouse
    without paying full-SDL cost.
    """
    ctx = info.context
    eff = effective_registry(
        ctx["registry"], ctx.get("jwt_payload"), ctx.get("policy_engine")
    )
    return [{"name": t.name, "description": t.description} for t in eff]


def _make_root_resolver(table_name: str):
    """Return a carrier dict — no DB call. Sub-field resolvers read ``where`` from it."""

    def resolve_root(_, _info, where: dict | None = None) -> dict:
        return {"where": where, "_table": table_name}

    return resolve_root


def _make_nodes_resolver(table_name: str):
    async def resolve_nodes(
        parent: dict,
        info,
        order_by: list[dict] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        db = ctx["db"]
        cache_cfg: CacheConfig = ctx["cache_config"]
        resolve_policy = _make_resolve_policy(ctx)

        try:
            stmt = compile_nodes_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=ctx["registry"],
                dialect=db.dialect_name,
                where=parent.get("where"),
                order_by=order_by,
                limit=limit,
                offset=offset,
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc

        logger.debug("nodes {}: {}", table_name, stmt)

        try:
            rows = await execute_with_cache(
                stmt,
                dialect_name=db.dialect_name,
                runner=db.execute,
                cfg=cache_cfg,
            )
        except SAPoolTimeoutError as exc:
            raise _pool_timeout_error(db) from exc

        logger.debug("nodes {} returned {} rows", table_name, len(rows))
        return rows

    return resolve_nodes


def _make_aggregate_field_resolver(field_name: str, tdef: TableDef):
    """Return a resolver that lazily computes ALL aggregates once per request.

    The first aggregate field resolver to run fires ``compile_aggregate_query``
    and stores an ``asyncio.Future`` on the carrier dict. All sibling aggregate
    field resolvers await that same Future, so only one DB round-trip occurs
    regardless of how many aggregate fields were selected.

    Exceptions are pre-translated to ``GraphQLError`` *before* being attached
    to the future so siblings awaiting the future surface the same client-
    facing error the originator did, not a raw ``PolicyError`` /
    ``SAPoolTimeoutError``.
    """
    all_agg_fields = {fname for fname, _ in agg_fields_for_table(tdef)}

    async def resolve_agg_field(parent: dict, info) -> Any:
        if _AGG_FUTURE_KEY not in parent:
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            parent[_AGG_FUTURE_KEY] = future
            ctx = info.context
            db = ctx["db"]
            cache_cfg: CacheConfig = ctx["cache_config"]
            resolve_policy = _make_resolve_policy(ctx)
            try:
                stmt = compile_aggregate_query(
                    tdef=tdef,
                    requested_agg_fields=all_agg_fields,
                    where=parent.get("where"),
                    resolve_policy=resolve_policy,
                )
                rows = await execute_with_cache(
                    stmt,
                    dialect_name=db.dialect_name,
                    runner=db.execute,
                    cfg=cache_cfg,
                )
            except PolicyError as exc:
                err = _to_graphql_error(exc)
                future.set_exception(err)
                raise err from exc
            except SAPoolTimeoutError as exc:
                err = _pool_timeout_error(db)
                future.set_exception(err)
                raise err from exc
            except Exception as exc:
                future.set_exception(exc)
                raise
            future.set_result(rows[0] if rows else {})

        row = await parent[_AGG_FUTURE_KEY]
        return row.get(field_name)

    return resolve_agg_field


def _make_group_resolver(table_name: str):
    async def resolve_group(
        parent: dict,
        info,
        order_by: list[dict] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        db = ctx["db"]
        cache_cfg: CacheConfig = ctx["cache_config"]
        resolve_policy = _make_resolve_policy(ctx)

        try:
            stmt = compile_group_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                where=parent.get("where"),
                order_by=order_by,
                limit=limit,
                offset=offset,
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc

        logger.debug("group {}: {}", table_name, stmt)

        try:
            rows = await execute_with_cache(
                stmt,
                dialect_name=db.dialect_name,
                runner=db.execute,
                cfg=cache_cfg,
            )
        except SAPoolTimeoutError as exc:
            raise _pool_timeout_error(db) from exc

        logger.debug("group {} returned {} rows", table_name, len(rows))
        return rows

    return resolve_group


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolve_policy(ctx: dict):
    policy_engine = ctx.get("policy_engine")
    jwt_payload = ctx.get("jwt_payload")
    if policy_engine is None:
        return None
    return functools.partial(policy_engine.evaluate, ctx=jwt_payload)


def _pool_timeout_error(db) -> GraphQLError:
    return GraphQLError(
        "database connection pool exhausted",
        extensions={
            "code": POOL_TIMEOUT_CODE,
            "retry_after": db._pool.retry_after,
        },
    )


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
