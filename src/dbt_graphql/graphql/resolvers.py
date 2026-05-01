from __future__ import annotations

import functools
from typing import Any

from ariadne import QueryType
from graphql import GraphQLError
from graphql.language import ListValueNode, ObjectValueNode, VariableNode
from loguru import logger
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

from ..cache.result import execute_with_cache
from ..compiler.query import compile_query
from ..config import CacheConfig
from ..schema.constants import AGGREGATE_FIELD
from .effective import effective_registry
from .policy import PolicyError

# GraphQL extension code paired with the HTTP handler's 503 elevation.
POOL_TIMEOUT_CODE = "POOL_TIMEOUT"

# Nested aggregate field names that need restructuring
_NESTED_AGG_FIELDS = frozenset(
    {
        AGGREGATE_FIELD,
    }
)


def _restructure_nested_aggregates(
    rows: list[dict[str, Any]],
    field_nodes: list,
) -> list[dict[str, Any]]:
    """Restructure flat aggregate results into nested GraphQL response format.

    When querying `orders { _aggregate { sum { price quantity } count } }`, the SQL returns
    flat keys like `{"_sum_price": 100, "_sum_quantity": 200, "_count": 10}`.
    This function restructures them to `{"_aggregate": {"sum": {"price": 100, "quantity": 200}, "count": 10}}`.
    """
    if not rows or not field_nodes:
        return rows

    # Get the selection set from the first field node
    selection = field_nodes[0]
    if not selection.selection_set:
        return rows

    # Find the _aggregate field and its nested selections
    agg_field_node = None
    for field in selection.selection_set.selections:
        if field.name.value == AGGREGATE_FIELD:
            agg_field_node = field
            break

    if agg_field_node is None or agg_field_node.selection_set is None:
        return rows

    # Build a map of operations to their selected columns
    # e.g., {"sum": ["price", "quantity"], "count": [], "count_distinct": ["action"]}
    op_selections: dict[str, list[str]] = {}
    for op_field in agg_field_node.selection_set.selections:
        op_name = op_field.name.value
        if op_field.selection_set:
            op_selections[op_name] = [
                f.name.value for f in op_field.selection_set.selections
            ]
        else:
            op_selections[op_name] = []

    # Restructure each row
    result_rows = []
    for row in rows:
        new_row = {"_aggregate": {}}
        for key, value in row.items():
            # Check if this key matches any aggregate operation pattern
            restructured = False

            # Special case: count_distinct keys must be checked before count
            # because "_count_distinct_action".startswith("_count_") is True
            if key.startswith("_count_distinct_"):
                if "count_distinct" in op_selections:
                    col_name = key[len("_count_distinct_") :]
                    op_cols = op_selections["count_distinct"]
                    if col_name in op_cols or not op_cols:
                        new_row["_aggregate"].setdefault("count_distinct", {})[
                            col_name
                        ] = value
                        restructured = True

            if not restructured:
                for op_name, op_cols in op_selections.items():
                    # Skip count_distinct - already handled above
                    if op_name == "count_distinct":
                        continue
                    # Handle bare aggregate keys like "_count" (no column suffix)
                    bare_key = f"_{op_name}"
                    if key == bare_key and not op_cols:
                        new_row["_aggregate"][op_name] = value
                        restructured = True
                        break
                    # Other ops have format: _sum_price, _avg_price, _count_email, etc.
                    prefix = f"_{op_name}_"
                    if key.startswith(prefix):
                        col_name = key[len(prefix) :]
                        if col_name:
                            if op_name not in new_row["_aggregate"]:
                                new_row["_aggregate"][op_name] = {}
                            new_row["_aggregate"][op_name][col_name] = value
                            restructured = True
                        break
            if not restructured:
                new_row[key] = value
        result_rows.append(new_row)

    return result_rows


def parse_order_by(info, arg_name="order_by"):
    """Read order_by from the AST in literal source order.

    Returns a list of (field_name, direction) tuples suitable for compile_query.
    Handles:
      - inline list form: order_by: [{col1: asc, col2: desc}] → ListValueNode
      - inline object form: order_by: {col1: asc, col2: desc} → ObjectValueNode
      - variable form: order_by: $vars → VariableNode
    """
    if not info.field_nodes:
        return []
    field = info.field_nodes[0]
    for arg in field.arguments:
        if arg.name.value != arg_name:
            continue
        v = arg.value
        if isinstance(v, ListValueNode):
            # Inline list form: [{col1: asc}, {col2: desc}]
            result = []
            for item in v.values:
                if isinstance(item, ObjectValueNode):
                    for f in item.fields:
                        result.append((f.name.value, f.value.value))
            return result
        if isinstance(v, ObjectValueNode):
            return [(f.name.value, f.value.value) for f in v.fields]
        if isinstance(v, VariableNode):
            var = info.variable_values.get(v.name.value, {}) or {}
            return list(var.items())
    return []


def create_query_type(registry) -> tuple[QueryType, list]:
    """Build the GraphQL ``Query`` resolver set.

    Returns a ``(QueryType, [])`` tuple. The object_types list is empty
    since we no longer use the {T}Result envelope pattern.
    """
    query_type = QueryType()

    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_root_resolver(name))

    query_type.set_field("_sdl", _resolve_sdl)
    query_type.set_field("_tables", _resolve_tables)
    return query_type, []


def _resolve_sdl(_, info, tables: list[str] | None = None) -> str:
    """Return the effective db.graphql SDL for the current caller.

    Produces policy-pruned SDL with full custom directives
    (@table, @column, @relation, @masked, @filtered).
    When tables is given, only those tables are emitted.
    """
    from .sdl.view import effective_document, render_sdl

    ctx = info.context
    eff_reg = effective_registry(
        ctx["registry"], ctx.get("jwt_payload"), ctx.get("policy_engine")
    )
    restrict_to = set(tables) if tables is not None else None
    doc = effective_document(ctx["source_doc"], eff_reg, restrict_to=restrict_to)
    return render_sdl(doc)


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
    """Return a resolver that executes a unified query and returns rows directly."""

    async def resolve_root(
        _,
        info,
        where: dict | None = None,
        order_by: dict | None = None,  # Ariadne coerces input object to dict
        limit: int | None = None,
        offset: int | None = None,
        distinct: list | None = None,
    ) -> list[dict[str, Any]]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        db = ctx["db"]
        cache_cfg: CacheConfig = ctx["cache_config"]
        resolve_policy = _make_resolve_policy(ctx)

        # Parse order_by from AST to get source order (list of tuples)
        order_by_parsed = parse_order_by(info)

        try:
            stmt = compile_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=ctx["registry"],
                dialect=db.dialect_name,
                where=where,
                order_by=order_by_parsed,
                limit=limit,
                offset=offset,
                distinct=distinct,
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc

        logger.debug("query {}: {}", table_name, stmt)

        try:
            rows = await execute_with_cache(
                stmt,
                dialect_name=db.dialect_name,
                runner=db.execute,
                cfg=cache_cfg,
            )
        except SAPoolTimeoutError as exc:
            raise _pool_timeout_error(db) from exc

        logger.debug("query {} returned {} rows", table_name, len(rows))

        # Restructure flat aggregate results into nested GraphQL format
        rows = _restructure_nested_aggregates(rows, info.field_nodes)

        return rows

    return resolve_root


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
