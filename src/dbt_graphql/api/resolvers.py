from __future__ import annotations

from typing import Any

from ariadne import QueryType
from graphql import GraphQLError

from ..compiler.query import compile_query
from .policy import PolicyError

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
        resolve_policy = None
        if policy_engine is not None:
            jwt_payload = ctx["jwt_payload"]
            resolve_policy = lambda t: policy_engine.evaluate(t, jwt_payload)  # noqa: E731

        try:
            stmt = compile_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=ctx["registry"],
                dialect=ctx["db"].dialect_name,
                limit=kwargs.get("limit"),
                offset=kwargs.get("offset"),
                where=kwargs.get("where"),
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc

        logger.debug("query {}: {}", table_name, stmt)
        rows = await ctx["db"].execute(stmt)
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
