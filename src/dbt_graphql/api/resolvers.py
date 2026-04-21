"""Ariadne resolvers that translate GraphQL queries to SQL.

Each ``Query`` field is a table name. The resolver extracts the selection set,
builds a SQL query via the compiler, executes it, and returns rows as dicts.

All shared state (registry, db) is passed through ``info.context``
so resolvers don't need to close over mutable objects.
"""

from __future__ import annotations

import logging
from typing import Any

from ariadne import QueryType

from ..compiler.query import compile_query

logger = logging.getLogger(__name__)


def create_query_type(registry) -> QueryType:
    """Return an Ariadne ``QueryType`` with a resolver for each table.

    Registers one resolver per table name. The resolver reads shared state
    from ``info.context`` (set by ``server.py``):
    - ``registry``: ``TableRegistry``
    - ``db``: ``DatabaseManager``
    """
    query_type = QueryType()

    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_resolver(name))

    return query_type


def _make_resolver(table_name: str):
    """Create a closure so each resolver knows its table name."""

    async def resolve_table(_, info, **kwargs) -> list[dict[str, Any]]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        if tdef is None:
            raise ValueError(f"Unknown table: {table_name}")

        stmt = compile_query(
            tdef=tdef,
            field_nodes=info.field_nodes,
            registry=ctx["registry"],
            dialect=ctx["db"].dialect_name,
            limit=kwargs.get("limit"),
            offset=kwargs.get("offset"),
            where=kwargs.get("where"),
        )
        logger.debug("query %s: %s", table_name, stmt)
        rows = await ctx["db"].execute(stmt)
        logger.debug("query %s returned %d rows", table_name, len(rows))
        return rows

    return resolve_table
