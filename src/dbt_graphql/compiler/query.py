"""Compile GraphQL field selections into SQLAlchemy Core queries.

Emits flat SELECT statements for single-table queries and correlated
subqueries for nested relations — no LATERAL joins, so this is safe for
Apache Doris.

Dialect-specific JSON functions are handled via SQLAlchemy's ``compiles``
extension so the query builder stays database-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import (
    Column,
    Select,
    and_,
    func,
    literal,
    literal_column,
    not_,
    null,
    or_,
    select,
    table,
    true,
)

from ..schema.models import ColumnDef, RelationDef, TableDef, TableRegistry
from ..schema.constants import AGGREGATE_FIELD
from ..schema.helpers import numeric_columns, scalar_columns
from ..compiler.operators import apply_comparison, AGG_FUNC_MAP, ORDER_BY_MAP
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.expression import FunctionElement

if TYPE_CHECKING:
    from ..graphql.policy import ResolvedPolicy

from ..graphql.policy import ColumnAccessDenied

ResolvePolicy = Callable[[str], "ResolvedPolicy"]


# ---------------------------------------------------------------------------
# Dialect-aware JSON functions via SQLAlchemy ``compiles``
# ---------------------------------------------------------------------------


class json_agg(FunctionElement):
    """Aggregate JSON values — compiles to the right function per dialect."""

    name = "json_agg"
    inherit_cache = True


class json_build_obj(FunctionElement):
    """Build a JSON object from key/value pairs."""

    name = "json_build_obj"
    inherit_cache = True


# --- json_agg per-dialect compilers ---


@compiles(json_agg, "mysql")
@compiles(json_agg, "mariadb")
def _mysql_json_agg(element, compiler, **kw):
    return "JSON_ARRAYAGG(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_agg, "postgresql")
def _pg_json_agg(element, compiler, **kw):
    return "JSONB_AGG(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_agg)
def _default_json_agg(element, compiler, **kw):
    return "JSON_ARRAYAGG(%s)" % compiler.process(element.clauses, **kw)


# --- json_build_obj per-dialect compilers ---


@compiles(json_build_obj, "mysql")
@compiles(json_build_obj, "mariadb")
def _standard_json_object(element, compiler, **kw):
    return "JSON_OBJECT(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_build_obj, "postgresql")
def _pg_json_build_obj(element, compiler, **kw):
    return "JSONB_BUILD_OBJECT(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_build_obj)
def _default_json_build_obj(element, compiler, **kw):
    return "JSON_OBJECT(%s)" % compiler.process(element.clauses, **kw)


# ---------------------------------------------------------------------------
# WHERE bool_exp support
# ---------------------------------------------------------------------------


def _where_to_clause(
    where: dict,
    aliased: Any,
    tdef: TableDef,
    resolved_policy: "ResolvedPolicy | None",
) -> Any:
    """Recursively compile a ``{T}_bool_exp`` dict to a SQLAlchemy clause."""
    clauses = []
    for key, value in where.items():
        # Handle combinators (_and, _or, _not)
        if key == "_and":
            clauses.append(
                and_(
                    *[
                        _where_to_clause(b, aliased, tdef, resolved_policy)
                        for b in value
                    ]
                )
            )
        elif key == "_or":
            clauses.append(
                or_(
                    *[
                        _where_to_clause(b, aliased, tdef, resolved_policy)
                        for b in value
                    ]
                )
            )
        elif key == "_not":
            clauses.append(
                not_(_where_to_clause(value, aliased, tdef, resolved_policy))
            )
        else:
            if resolved_policy is not None and not resolved_policy.is_column_allowed(
                key
            ):
                raise ColumnAccessDenied(tdef.name, [key])
            col = aliased.c[key]
            for op, operand in value.items():
                clauses.append(apply_comparison(col, op, operand))
    return and_(*clauses) if clauses else true()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _table_from_def(tdef: TableDef):
    cols = [Column(c.name) for c in tdef.columns]
    return table(tdef.table, *cols, schema=tdef.schema or None)


def _mask_column(mask_sql: str | None, col_name: str) -> ClauseElement:
    """Trusted SQL fragment from security.policies; None produces SQL NULL."""
    if mask_sql is None:
        return null().label(col_name)
    return literal_column(mask_sql).label(col_name)


def _extract_scalar_fields(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
) -> tuple[list[str], list[tuple[ColumnDef, RelationDef, TableDef]]]:
    """Split selected fields into scalars and relations."""
    scalars: list[str] = []
    relations: list[tuple[ColumnDef, RelationDef, TableDef]] = []

    for node in field_nodes:
        name = node.name.value
        col = next((c for c in tdef.columns if c.name == name), None)
        if col is None:
            continue
        if col.relation:
            target = registry.get(col.relation.target_model)
            if target:
                relations.append((col, col.relation, target))
            continue
        scalars.append(name)

    return scalars, relations


def _enforce_strict_columns(
    table_name: str,
    requested: list[str],
    policy: "ResolvedPolicy",
) -> None:
    """Raise ``ColumnAccessDenied`` if any requested column is unauthorized."""
    denied = [col for col in requested if not policy.is_column_allowed(col)]
    if denied:
        raise ColumnAccessDenied(table_name, denied)


def _collect_field_names(field_nodes: list) -> list[str]:
    """Return the list of field names selected in the first field node's selection set.

    Returns a list (not set) to preserve AST order for cache key differentiation.
    """
    if not field_nodes:
        return []
    sel = field_nodes[0]
    if sel.selection_set is None:
        return []
    return [f.name.value for f in sel.selection_set.selections]


# ---------------------------------------------------------------------------
# Unified compile_query
# ---------------------------------------------------------------------------


def _is_agg_field(fname: str) -> bool:
    """Return True if fname is the _aggregate wrapper field."""
    return fname == AGGREGATE_FIELD


def compile_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    dialect: str = "",
    where: dict | None = None,
    order_by: list[tuple[str, str]] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    distinct: bool | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """Unified query compiler handling row-only, aggregate-only, and mixed queries.

    Partitions fields into:
      - dim_cols: real table columns (dimension fields)
      - agg_cols: aggregate fields (count, count_<col>, sum_<col>, avg_<col>, etc.)
      - row_only: True when only dimension columns are selected (no aggregates)

    Produces three SQL shapes:
      - row_only:    SELECT dim_cols FROM t WHERE ... ORDER BY ... LIMIT ...
      - agg_cols only: SELECT agg_cols FROM t WHERE ...
      - both:        SELECT dim_cols, agg_cols FROM t WHERE ... GROUP BY dim_cols ...

    Mutual exclusivity checks:
      - distinct + agg_cols → ValueError
      - relation fields + agg_cols → ValueError
    """
    selection = field_nodes[0] if field_nodes else None
    if selection is None:
        return select()

    sub_fields = selection.selection_set.selections if selection.selection_set else []
    scalars, relations = _extract_scalar_fields(tdef, sub_fields, registry)

    resolved_policy: "ResolvedPolicy | None" = None
    if resolve_policy is not None:
        resolved_policy = resolve_policy(tdef.name)

    requested = _collect_field_names(field_nodes)
    dim_col_names = {c.name for c in tdef.columns if not c.is_array}

    dim_cols = [f for f in requested if f in dim_col_names]
    agg_cols = [f for f in requested if _is_agg_field(f)]
    row_only = len(agg_cols) == 0

    # Mutual exclusivity: distinct + aggregates
    if distinct and agg_cols:
        raise ValueError("distinct and aggregate fields cannot be selected together")

    # Mutual exclusivity: relations + aggregates
    if relations and agg_cols:
        raise ValueError(
            "aggregate fields cannot be selected alongside relation fields"
        )

    sa_table = _table_from_def(tdef)
    aliased = sa_table.alias("_uq")

    # Build projections
    projections: list = []
    agg_projections: list = []

    # Dimension columns
    masks = resolved_policy.masks if resolved_policy is not None else {}

    # Pre-validate all dimension columns to collect all denied ones (backward compat)
    if resolved_policy is not None:
        denied = [col for col in dim_cols if not resolved_policy.is_column_allowed(col)]
        if denied:
            raise ColumnAccessDenied(tdef.name, denied)

    for name in dim_cols:
        if resolved_policy is not None and not resolved_policy.is_column_allowed(name):
            raise ColumnAccessDenied(tdef.name, [name])
        if name in masks:
            projections.append(_mask_column(masks[name], name))
        else:
            projections.append(aliased.c[name].label(name))

    # Aggregate columns
    col_map = {c.name: c for c in tdef.columns}

    for fname in sorted(agg_cols):
        if fname == AGGREGATE_FIELD:
            # Get operations inside _aggregate and their nested column selections
            agg_selection = None
            for node in field_nodes:
                sel = node
                if sel.selection_set:
                    for f in sel.selection_set.selections:
                        if f.name.value == AGGREGATE_FIELD:
                            agg_selection = f
                            break

            if agg_selection is None or agg_selection.selection_set is None:
                continue

            # Process each operation inside _aggregate
            for op_field in agg_selection.selection_set.selections:
                op_name = op_field.name.value
                op_nested_cols = []
                if op_field.selection_set:
                    op_nested_cols = [
                        f.name.value for f in op_field.selection_set.selections
                    ]

                if op_name == "count":
                    # COUNT(*) - no column needed
                    if op_nested_cols:
                        # count with specific columns - COUNT(col) for each
                        for col_name in op_nested_cols:
                            col_def = col_map.get(col_name)
                            if col_def is None or col_def.is_array:
                                continue
                            if (
                                resolved_policy is not None
                                and not resolved_policy.is_column_allowed(col_name)
                            ):
                                raise ColumnAccessDenied(tdef.name, [col_name])
                            internal_name = f"_count_{col_name}"
                            agg_projections.append(
                                func.count(aliased.c[col_name]).label(internal_name)
                            )
                    else:
                        # Plain COUNT(*)
                        agg_projections.append(func.count().label("_count"))
                    continue

                if op_name == "count_distinct":
                    # COUNT(DISTINCT col) for each selected column
                    for col_name in op_nested_cols:
                        col_def = col_map.get(col_name)
                        if col_def is None or col_def.is_array:
                            continue
                        if (
                            resolved_policy is not None
                            and not resolved_policy.is_column_allowed(col_name)
                        ):
                            raise ColumnAccessDenied(tdef.name, [col_name])
                        internal_name = f"_count_distinct_{col_name}"
                        agg_projections.append(
                            func.count(aliased.c[col_name].distinct()).label(
                                internal_name
                            )
                        )
                    continue

                # Map operation name to SQL function
                agg_fn = AGG_FUNC_MAP.get(op_name)
                if agg_fn is None:
                    continue

                # For sum/avg/stddev/var - only use numeric columns
                # For min/max - use all non-array columns
                valid_cols = op_nested_cols
                if not valid_cols:
                    # Default columns based on operation type
                    if op_name in ("sum", "avg", "stddev", "var"):
                        valid_cols = [c.name for c in numeric_columns(tdef.columns)]
                    else:
                        valid_cols = [c.name for c in scalar_columns(tdef.columns)]

                for col_name in valid_cols:
                    col_def = col_map.get(col_name)
                    if col_def is None or col_def.is_array:
                        continue
                    if (
                        resolved_policy is not None
                        and not resolved_policy.is_column_allowed(col_name)
                    ):
                        raise ColumnAccessDenied(tdef.name, [col_name])
                    # Internal naming: _sum_price, _avg_price, etc.
                    internal_name = f"_{op_name}_{col_name}"
                    agg_projections.append(
                        agg_fn(aliased.c[col_name]).label(internal_name)
                    )

    # Build the statement based on query shape
    if row_only:
        # Shape 1: row-only (no aggregates)
        all_projections = projections
        stmt = select(*all_projections).select_from(aliased)

        if (
            resolved_policy is not None
            and resolved_policy.row_filter_clause is not None
        ):
            stmt = stmt.where(resolved_policy.row_filter_clause)

        if where:
            stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

        if order_by:
            for col_name, direction in order_by:
                order_fn = ORDER_BY_MAP.get(direction)
                if order_fn is None:
                    raise ValueError(f"Unknown order_by direction: {direction!r}")
                if (
                    resolved_policy is not None
                    and not resolved_policy.is_column_allowed(col_name)
                ):
                    raise ColumnAccessDenied(tdef.name, [col_name])
                stmt = stmt.order_by(order_fn(aliased.c[col_name]))

        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

        if distinct:
            stmt = stmt.distinct()

    elif not dim_cols:
        # Shape 2: aggregates only (no dimensions)
        all_projections = agg_projections
        if not all_projections:
            all_projections = [func.count().label("_count")]

        stmt = select(*all_projections).select_from(aliased)

        if (
            resolved_policy is not None
            and resolved_policy.row_filter_clause is not None
        ):
            stmt = stmt.where(resolved_policy.row_filter_clause)

        if where:
            stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

    else:
        # Shape 3: both dimensions and aggregates → GROUP BY
        all_projections = projections + agg_projections
        group_cols = [aliased.c[d] for d in dim_cols]

        stmt = select(*all_projections).select_from(aliased)
        if group_cols:
            stmt = stmt.group_by(*group_cols)

        if (
            resolved_policy is not None
            and resolved_policy.row_filter_clause is not None
        ):
            stmt = stmt.where(resolved_policy.row_filter_clause)

        if where:
            stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

        if order_by:
            for col_name, direction in order_by:
                order_fn = ORDER_BY_MAP.get(direction)
                if order_fn is None:
                    raise ValueError(f"Unknown order_by direction: {direction!r}")
                if col_name in dim_col_names:
                    if (
                        resolved_policy is not None
                        and not resolved_policy.is_column_allowed(col_name)
                    ):
                        raise ColumnAccessDenied(tdef.name, [col_name])
                    stmt = stmt.order_by(order_fn(aliased.c[col_name]))
                elif col_name == AGGREGATE_FIELD:
                    # For _aggregate order_by, use the first available aggregate projection
                    # The caller should specify which aggregate operation to order by
                    existing_labels = [
                        p._label for p in all_projections if hasattr(p, "_label")
                    ]
                    # Try to find a reasonable aggregate column to order by
                    # Prefer _count if available, otherwise first aggregate
                    order_label = None
                    if "_count" in existing_labels:
                        order_label = "_count"
                    elif existing_labels:
                        order_label = existing_labels[0]
                    if order_label:
                        stmt = stmt.order_by(order_fn(literal_column(order_label)))
                elif col_name.startswith("_") and not col_name.startswith("__"):
                    # Might be an aggregate column - check if it matches aggregate patterns
                    # Pattern: _<op>_<col> or _count or _count_distinct_<col>
                    is_aggregate = False
                    order_label = None

                    # Check all possible aggregate label patterns
                    if col_name == "_count":
                        is_aggregate = True
                        if "_count" in [
                            p._label for p in all_projections if hasattr(p, "_label")
                        ]:
                            order_label = "_count"
                    elif col_name.startswith("_count_distinct_"):
                        col = col_name[len("_count_distinct_") :]
                        potential_label = f"_count_distinct_{col}"
                        if potential_label in [
                            p._label for p in all_projections if hasattr(p, "_label")
                        ]:
                            order_label = potential_label
                            is_aggregate = True
                    else:
                        # Check for _sum_, _avg_, _min_, _max_, _stddev_, _var_ patterns
                        for prefix in (
                            "_sum_",
                            "_avg_",
                            "_min_",
                            "_max_",
                            "_stddev_",
                            "_var_",
                        ):
                            if col_name.startswith(prefix):
                                col = col_name[len(prefix) :]
                                potential_label = f"{prefix}{col}"
                                if potential_label in [
                                    p._label
                                    for p in all_projections
                                    if hasattr(p, "_label")
                                ]:
                                    order_label = potential_label
                                    is_aggregate = True
                                break

                    if is_aggregate and order_label:
                        stmt = stmt.order_by(order_fn(literal_column(order_label)))
                        continue
                else:
                    if (
                        resolved_policy is not None
                        and not resolved_policy.is_column_allowed(col_name)
                    ):
                        raise ColumnAccessDenied(tdef.name, [col_name])
                    stmt = stmt.order_by(order_fn(aliased.c[col_name]))

        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

    return stmt


def _build_correlated_subquery(
    parent_aliased,
    parent_fk: str,
    rel: RelationDef,
    target: TableDef,
    child_fields: list,
    registry: TableRegistry,
    dialect: str = "",
    depth: int = 1,
    visited: frozenset[str] = frozenset(),
    max_depth: int | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """Build a correlated subquery for a nested relation, recursively.

    Uses SQLAlchemy expressions so dialect-specific compilation
    (JSON functions, quoting, etc.) is handled automatically.
    Aliases each level as child_1, child_2, … to avoid name collisions.

    When ``resolve_policy`` is provided, the policy for ``target`` is
    evaluated here — denying the table, rejecting unauthorized columns,
    applying masks to the JSON payload, and appending the row filter to
    the subquery's WHERE clause. Without this, nested GraphQL selections
    would bypass the policy engine entirely.
    """
    if max_depth is not None and depth > max_depth:
        raise ValueError(f"Relation nesting exceeds maximum depth of {max_depth}")
    if target.name in visited:
        raise ValueError(
            f"Circular relation detected: '{target.name}' is already in the join path"
        )

    child_scalars, child_relations = _extract_scalar_fields(
        target, child_fields, registry
    )

    policy: "ResolvedPolicy | None" = None
    if resolve_policy is not None:
        policy = resolve_policy(target.name)
        _enforce_strict_columns(target.name, child_scalars, policy)

    child_table = _table_from_def(target).alias(f"child_{depth}")
    new_visited = visited | {target.name}

    masks = policy.masks if policy is not None else {}

    # JSON_OBJECT('col', child_N.col, ...) — masks replace the column ref.
    json_args: list = []
    for col_name in child_scalars:
        json_args.append(literal(col_name))
        if col_name in masks:
            mask_sql = masks[col_name]
            json_args.append(null() if mask_sql is None else literal_column(mask_sql))
        else:
            json_args.append(child_table.c[col_name])

    # Recurse for nested relations
    for child_col, child_rel, child_target in child_relations:
        child_field_node = next(
            (f for f in child_fields if f.name.value == child_col.name), None
        )
        if child_field_node is None or child_field_node.selection_set is None:
            continue
        nested_sub = _build_correlated_subquery(
            parent_aliased=child_table,
            parent_fk=child_col.name,
            rel=child_rel,
            target=child_target,
            child_fields=child_field_node.selection_set.selections,
            registry=registry,
            dialect=dialect,
            depth=depth + 1,
            visited=new_visited,
            max_depth=max_depth,
            resolve_policy=resolve_policy,
        )
        json_args.append(literal(child_col.name))
        json_args.append(nested_sub.scalar_subquery())

    inner = json_build_obj(*json_args)
    agg = json_agg(inner)

    # Build join predicate — composite FK if to_columns has multiple entries
    if rel.to_columns and len(rel.to_columns) > 1:
        parent_cols = rel.from_columns if rel.from_columns else [parent_fk]
        predicate = and_(
            *(
                child_table.c[tc] == parent_aliased.c[fc]
                for fc, tc in zip(parent_cols, rel.to_columns)
            )
        )
    else:
        predicate = child_table.c[rel.target_column] == parent_aliased.c[parent_fk]

    stmt = select(agg).where(predicate).correlate(parent_aliased)

    if policy is not None and policy.row_filter_clause is not None:
        stmt = stmt.where(policy.row_filter_clause)

    return stmt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
