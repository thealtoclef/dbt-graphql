from __future__ import annotations

from typing import Any

from ariadne import QueryType

from ..compiler.query import compile_query

from loguru import logger


def create_query_type(registry) -> QueryType:
    query_type = QueryType()

    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_resolver(name))

    return query_type


def _make_resolver(table_name: str):

    async def resolve_table(_, info, **kwargs) -> list[dict[str, Any]]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        if tdef is None:
            raise ValueError(f"Unknown table: {table_name}")

        policy_engine = ctx.get("policy_engine")
        resolved_policy = (
            policy_engine.evaluate(table_name, ctx["jwt_payload"])
            if policy_engine is not None
            else None
        )

        stmt = compile_query(
            tdef=tdef,
            field_nodes=info.field_nodes,
            registry=ctx["registry"],
            dialect=ctx["db"].dialect_name,
            limit=kwargs.get("limit"),
            offset=kwargs.get("offset"),
            where=kwargs.get("where"),
            resolved_policy=resolved_policy,
        )
        logger.debug("query {}: {}", table_name, stmt)
        rows = await ctx["db"].execute(stmt)
        logger.debug("query {} returned {} rows", table_name, len(rows))
        return rows

    return resolve_table
