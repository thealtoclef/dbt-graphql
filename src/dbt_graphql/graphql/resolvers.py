from __future__ import annotations

import functools
from typing import Any

from ariadne import QueryType
from graphql import GraphQLError
from graphql.language import ListValueNode, ObjectValueNode, VariableNode
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

from ..cache.result import execute_with_cache
from ..compiler.query import compile_query, compile_connection_query
from ..config import CacheConfig, GraphQLConfig
from ..schema.constants import AGGREGATE_FIELD
from ..schema.models import TableDef
from .cursors import encode_cursor, decode_cursor, _query_fingerprint
from .effective import effective_registry
from .policy import PolicyError

# GraphQL extension code paired with the HTTP handler's 503 elevation.
POOL_TIMEOUT_CODE = "POOL_TIMEOUT"


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

    Returns a ``(QueryType, [])`` tuple. Each table field returns a
    ``{T}Result`` connection wrapper with ``nodes`` and ``pageInfo``.
    """
    query_type = QueryType()

    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_connection_resolver(name))

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


def _extract_nodes_selection(field_nodes):
    """Extract the inner `nodes { ... }` selection set from the connection wrapper."""
    if not field_nodes:
        raise GraphQLError(
            "Missing field selection for connection query.",
            extensions={"code": "INTERNAL_ERROR"},
        )
    top = field_nodes[0]
    if not top.selection_set:
        raise GraphQLError(
            "Connection query requires a `nodes` selection.",
            extensions={"code": "MISSING_NODES_SELECTION"},
        )
    for child in top.selection_set.selections:
        if child.name.value == "nodes" and child.selection_set:
            return [child]
    raise GraphQLError(
        "Connection query must include a `nodes { ... }` selection. "
        "Add `nodes { ... }` to your query.",
        extensions={"code": "MISSING_NODES_SELECTION"},
    )


def _make_connection_resolver(table_name: str):
    """Return a resolver that executes a connection query with cursor pagination."""

    async def resolve_connection(
        _,
        info,
        where=None,
        order_by=None,
        first=None,
        after=None,
        distinct=None,
    ):
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        db = ctx["db"]
        cache_cfg: CacheConfig = ctx["cache_config"]
        resolve_policy = _make_resolve_policy(ctx)
        graphql_config: GraphQLConfig = ctx["graphql_config"]

        inner_field_nodes = _extract_nodes_selection(info.field_nodes)

        order_by_parsed = parse_order_by(info, arg_name="order_by")

        # Early check: order_by columns must be in the nodes selection
        if order_by_parsed:
            selected_cols = _extract_selected_column_names(inner_field_nodes)
            order_by_col_set = {col for col, _ in order_by_parsed}
            missing = order_by_col_set - selected_cols
            if missing:
                raise GraphQLError(
                    f"order_by columns not in the nodes selection: {sorted(missing)}.",
                    extensions={"code": "ORDER_BY_NOT_IN_SELECTION"},
                )

        if after and not order_by_parsed:
            raise GraphQLError(
                "Cannot use 'after' without 'order_by'.",
                extensions={"code": "CURSOR_REQUIRES_ORDER_BY"},
            )

        effective_first = (
            first if first is not None else graphql_config.query_default_limit
        )
        if graphql_config.query_max_limit is not None:
            effective_first = min(effective_first, graphql_config.query_max_limit)

        page_info_selected = _page_info_selected(info.field_nodes)

        if page_info_selected and not order_by_parsed:
            raise GraphQLError(
                "Cannot select 'pageInfo' without 'order_by'.",
                extensions={"code": "ORDER_BY_REQUIRED"},
            )

        # Compute GROUP BY columns for cursor stale detection + uniqueness validation.
        # When _aggregate is selected, the compiler groups by all non-aggregate
        # selected columns.  Changing these between pages changes the result set.
        effective_distinct = distinct if distinct is not None else False
        requested = _extract_selected_column_names(inner_field_nodes)
        has_aggregate = AGGREGATE_FIELD in requested
        group_by_cols: set[str] | None = None
        if has_aggregate:
            col_names = {c.name for c in tdef.columns if not c.is_array}
            group_by_cols = requested.intersection(col_names)

        if order_by_parsed:
            _validate_order_by_uniqueness(tdef, order_by_parsed, group_by_cols)

        if not order_by_parsed:
            try:
                stmt = compile_query(
                    tdef=tdef,
                    field_nodes=inner_field_nodes,
                    registry=ctx["registry"],
                    where=where,
                    order_by=order_by_parsed,
                    limit=effective_first,
                    distinct=effective_distinct,
                    resolve_policy=resolve_policy,
                )
                rows = await execute_with_cache(
                    stmt,
                    dialect_name=db.dialect_name,
                    runner=db.execute,
                    cfg=cache_cfg,
                )
            except PolicyError as e:
                raise _to_graphql_error(e)
            except SAPoolTimeoutError:
                raise _pool_timeout_error(db)
            rows = _restructure_nested_aggregates(rows, inner_field_nodes)
            return {
                "nodes": rows,
                "pageInfo": {
                    "endCursor": None,
                    "hasNextPage": False,
                },
            }

        # Decode cursor if provided
        cursor_values = None
        if after:
            payload = decode_cursor(after)
            if not payload:
                raise GraphQLError(
                    "Invalid or expired cursor", extensions={"code": "INVALID_CURSOR"}
                )
            # Verify cursor matches current query parameters
            if payload.digest != _query_fingerprint(
                order_by=order_by_parsed,
                where=where,
                distinct=effective_distinct,
                group_by_cols=group_by_cols,
            ):
                raise GraphQLError(
                    "Cursor was created for a different query signature. "
                    "Start a fresh query without 'after'.",
                    extensions={"code": "CURSOR_STALE"},
                )
            cursor_values = payload.values

        # Compile + execute with LIMIT first+1
        try:
            stmt = compile_connection_query(
                tdef=tdef,
                field_nodes=inner_field_nodes,
                registry=ctx["registry"],
                where=where,
                order_by=order_by_parsed,
                cursor_values=cursor_values,
                limit=effective_first,
                distinct=effective_distinct,
                resolve_policy=resolve_policy,
            )
            rows = await execute_with_cache(
                stmt,
                dialect_name=db.dialect_name,
                runner=db.execute,
                cfg=cache_cfg,
            )
        except PolicyError as e:
            raise _to_graphql_error(e)
        except SAPoolTimeoutError:
            raise _pool_timeout_error(db)
        rows = _restructure_nested_aggregates(rows, inner_field_nodes)

        has_next_page = len(rows) > effective_first
        if has_next_page:
            rows = rows[:effective_first]

        # Build cursors using order_by columns
        end_cursor = None
        if rows:
            last_row = rows[-1]
            cursor_vals = {col: last_row[col] for col, _ in order_by_parsed}
            end_cursor = encode_cursor(
                order_by_parsed=order_by_parsed,
                cursor_values=cursor_vals,
                where=where,
                distinct=effective_distinct,
                group_by_cols=group_by_cols,
            )

        return {
            "nodes": rows,
            "pageInfo": {
                "endCursor": end_cursor,
                "hasNextPage": has_next_page,
            },
        }

    return resolve_connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page_info_selected(field_nodes: list) -> bool:
    """Check if pageInfo is selected in the GraphQL query."""
    if not field_nodes:
        return False
    top = field_nodes[0]
    if not top.selection_set:
        return False
    for child in top.selection_set.selections:
        if child.name.value == "pageInfo":
            return True
    return False


def _extract_selected_column_names(field_nodes: list) -> set[str]:
    """Extract column names from the nodes { ... } selection set."""
    if not field_nodes:
        return set()
    sel = field_nodes[0]
    if not sel.selection_set:
        return set()
    return {f.name.value for f in sel.selection_set.selections}


def _validate_order_by_uniqueness(
    tdef: TableDef,
    order_by_parsed: list[tuple[str, str]],
    group_by_cols: set[str] | None = None,
) -> None:
    """Validate that the set of order_by columns contains a unique key for stable cursors.

    See docs/pagination.md §9 for the full rule table.
    """
    order_by_cols = {col for col, _ in order_by_parsed}
    pk_cols = {col.name for col in tdef.columns if col.is_pk}
    unique_cols = {col.name for col in tdef.columns if col.is_unique}

    # 1. Covers all PK columns
    if pk_cols and pk_cols.issubset(order_by_cols):
        return

    # 2. Contains at least one unique-constraint column
    if order_by_cols.intersection(unique_cols):
        return

    # 3. (Aggregate only) covers all GROUP BY dimensions
    if group_by_cols and group_by_cols.issubset(order_by_cols):
        return

    # No condition met — build an actionable error.
    got = [col for col, _ in order_by_parsed]
    hints: list[str] = []

    if pk_cols:
        missing = sorted(pk_cols - order_by_cols)
        hints.append(f"include PK columns {sorted(pk_cols)} (missing: {missing})")
    if unique_cols:
        candidates = sorted(unique_cols - order_by_cols)
        if candidates:
            hints.append(f"include any unique-constraint column {candidates}")
    if group_by_cols is not None:
        missing = sorted(group_by_cols - order_by_cols)
        if missing:
            hints.append(
                f"include all GROUP BY dimension columns {sorted(group_by_cols)} "
                f"(missing: {missing})"
            )

    if not hints:
        hints.append(
            "table has no PK, unique, or GROUP BY columns to form a unique key"
        )

    raise GraphQLError(
        f"order_by does not guarantee a stable cursor — "
        f"columns do not form a unique key. Got: {got}. "
        f"To fix, {'; or '.join(hints)}.",
        extensions={"code": "CURSOR_ORDER_BY_NOT_UNIQUE"},
    )


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
