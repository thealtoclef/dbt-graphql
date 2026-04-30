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
    asc,
    desc,
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

from ..sql_ops import apply_comparison
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.expression import FunctionElement

if TYPE_CHECKING:
    from ..graphql.policy import ResolvedPolicy

from ..graphql.policy import ColumnAccessDenied
from ..formatter.schema import ColumnDef, RelationDef, TableDef, TableRegistry

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
# ORDER BY support
# ---------------------------------------------------------------------------

_ORDER_BY_MAP: dict[str, Any] = {
    "asc": lambda c: asc(c),
    "desc": lambda c: desc(c),
}


def _compile_order_by(
    order_by: list[dict],
    aliased: Any,
    tdef: TableDef,
    resolved_policy: "ResolvedPolicy | None",
) -> list:
    clauses = []
    for item in order_by:
        for col_name, direction in item.items():
            if resolved_policy is not None and not resolved_policy.is_column_allowed(
                col_name
            ):
                raise ColumnAccessDenied(tdef.name, [col_name])
            order_fn = _ORDER_BY_MAP.get(direction)
            if order_fn is None:
                raise ValueError(f"Unknown order_by direction: {direction!r}")
            clauses.append(order_fn(aliased.c[col_name]))
    return clauses


# ---------------------------------------------------------------------------
# Aggregate support
# ---------------------------------------------------------------------------

_AGG_FUNC_MAP: dict[str, Any] = {
    "sum": func.sum,
    "avg": func.avg,
    "stddev": func.stddev,
    "var": func.variance,
    "min": func.min,
    "max": func.max,
}

_NUMERIC_GQL_TYPES = frozenset({"Int", "Float"})

# Aggregate field names live in the same namespace as real columns on the
# ``{T}_group`` row type. Collisions (e.g. a dbt column literally named
# ``count`` or ``sum_amount``) are rejected at boot in
# ``create_graphql_subapp`` — see ``AGGREGATE_FIELD_NAMES`` plumbing there.
COUNT_FIELD = "count"
_AGG_PREFIXES = ("sum_", "avg_", "stddev_", "var_", "min_", "max_")


def agg_fields_for_table(tdef: TableDef) -> list[tuple[str, str]]:
    """Return ``[(field_name, gql_scalar)]`` for all aggregate fields of a table.

    Each tuple is ``(synthetic field name on {T}Result/{T}_group, GraphQL
    scalar this aggregate returns)`` — the second element is what the
    SDL emits to the right of the colon (``count: Int``,
    ``sum_Total: Float``, ``min_BillingState: String``, …).

    Always starts with ``("count", "Int")`` — ``COUNT(*)`` is always an
    integer regardless of column types. Numeric columns (Int, Float, ID)
    add sum/avg/stddev/var (always Float, since e.g. AVG of Ints is
    fractional) plus min/max (preserving the column's own scalar). Other
    columns (String, Boolean, …) get min/max only — lexicographic for
    strings, lattice min/max for booleans.

    Names are emitted as plain identifiers (no leading underscore) — they
    are public surface, not private. Double-underscore is reserved by the
    GraphQL spec for introspection (``__schema``); single-underscore has
    no formal meaning but reads as "internal", which these public-facing
    aggregate fields are not. We rely on the boot-time collision guard in
    ``create_graphql_subapp`` to reject any dbt column whose name matches
    a synthetic aggregate field.
    """
    fields: list[tuple[str, str]] = [(COUNT_FIELD, "Int")]
    for col in tdef.columns:
        if col.is_array:
            continue
        t = col.gql_type
        if t in _NUMERIC_GQL_TYPES:
            min_max_type = "Float" if t == "Float" else "Int"
            for fn in ("sum", "avg", "stddev", "var"):
                fields.append((f"{fn}_{col.name}", "Float"))
            fields.append((f"min_{col.name}", min_max_type))
            fields.append((f"max_{col.name}", min_max_type))
        else:
            fields.append((f"min_{col.name}", t))
            fields.append((f"max_{col.name}", t))
    return fields


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


def _collect_field_names(field_nodes: list) -> set[str]:
    """Return the set of field names selected in the first field node's selection set."""
    if not field_nodes:
        return set()
    sel = field_nodes[0]
    if sel.selection_set is None:
        return set()
    return {f.name.value for f in sel.selection_set.selections}


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


def compile_nodes_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    dialect: str = "",
    limit: int | None = None,
    offset: int | None = None,
    where: dict | None = None,
    order_by: list[dict] | None = None,
    max_depth: int | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """Build a SQLAlchemy Core ``Select`` for row data with bool_exp WHERE and ORDER BY.

    ``where`` is a ``{T}_bool_exp`` dict (recursive operators like ``_eq``,
    ``_and``, ``_or``, etc.). ``order_by`` is a list of single-key dicts
    mapping column name to an ``order_by`` enum value.

    ``max_depth`` caps relation nesting (None = unlimited). Cycles in the
    query selection are always rejected regardless of this setting.

    ``resolve_policy`` is a callable that maps a table name to its
    ``ResolvedPolicy`` (or raises ``TableAccessDenied``). When provided,
    policy is enforced at every table visited by the query — including
    nested relations.
    """
    selection = field_nodes[0] if field_nodes else None
    if selection is None:
        return select()

    sub_fields = selection.selection_set.selections if selection.selection_set else []
    scalars, relations = _extract_scalar_fields(tdef, sub_fields, registry)

    resolved_policy: "ResolvedPolicy | None" = None
    if resolve_policy is not None:
        resolved_policy = resolve_policy(tdef.name)
        _enforce_strict_columns(tdef.name, scalars, resolved_policy)

    sa_table = _table_from_def(tdef)
    aliased = sa_table.alias("_parent")

    masks = resolved_policy.masks if resolved_policy is not None else {}
    cols: list = []
    for name in scalars:
        if name in masks:
            cols.append(_mask_column(masks[name], name))
        else:
            cols.append(aliased.c[name].label(name))

    for col, rel, target in relations:
        child_field_node = next(
            (f for f in sub_fields if f.name.value == col.name), None
        )
        if child_field_node is None or child_field_node.selection_set is None:
            continue

        child_fields = child_field_node.selection_set.selections
        subquery = _build_correlated_subquery(
            parent_aliased=aliased,
            parent_fk=col.name,
            rel=rel,
            target=target,
            child_fields=child_fields,
            registry=registry,
            dialect=dialect,
            max_depth=max_depth,
            resolve_policy=resolve_policy,
        )
        cols.append(subquery.label(col.name))

    stmt = select(*cols).select_from(aliased)

    if resolved_policy is not None and resolved_policy.row_filter_clause is not None:
        stmt = stmt.where(resolved_policy.row_filter_clause)

    if where:
        stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

    if order_by:
        for clause in _compile_order_by(order_by, aliased, tdef, resolved_policy):
            stmt = stmt.order_by(clause)

    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)

    return stmt


def compile_aggregate_query(
    tdef: TableDef,
    requested_agg_fields: set[str],
    where: dict | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """Build a SELECT of aggregate projections over the filtered set.

    ``requested_agg_fields`` is the set of flat names to compute, e.g.
    ``{"count", "sum_Total", "avg_Total"}``. Only those aggregates appear
    in the SELECT — unselected ones are skipped.
    """
    sa_table = _table_from_def(tdef)
    aliased = sa_table.alias("_agg")

    resolved_policy: "ResolvedPolicy | None" = None
    if resolve_policy is not None:
        resolved_policy = resolve_policy(tdef.name)

    projections: list = []

    if COUNT_FIELD in requested_agg_fields:
        projections.append(func.count().label(COUNT_FIELD))

    col_map = {c.name: c for c in tdef.columns}

    for fname in sorted(requested_agg_fields):
        if fname == COUNT_FIELD:
            continue
        for prefix in _AGG_PREFIXES:
            if fname.startswith(prefix):
                col_name = fname[len(prefix) :]
                col_def = col_map.get(col_name)
                if col_def is None or col_def.is_array:
                    break
                if (
                    resolved_policy is not None
                    and not resolved_policy.is_column_allowed(col_name)
                ):
                    raise ColumnAccessDenied(tdef.name, [col_name])
                agg_fn_name = prefix.strip("_")
                agg_fn = _AGG_FUNC_MAP.get(agg_fn_name)
                if agg_fn is not None:
                    projections.append(agg_fn(aliased.c[col_name]).label(fname))
                break

    if not projections:
        projections = [func.count().label(COUNT_FIELD)]

    stmt = select(*projections).select_from(aliased)

    if resolved_policy is not None and resolved_policy.row_filter_clause is not None:
        stmt = stmt.where(resolved_policy.row_filter_clause)

    if where:
        stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

    return stmt


def compile_group_query(
    tdef: TableDef,
    field_nodes: list,
    where: dict | None = None,
    order_by: list[dict] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """GROUP BY query with auto-derived grouping keys (Cube pattern).

    GROUP BY columns are inferred from whichever dimension fields appear in
    the selection set — fields whose name matches a table column and does not
    start with an aggregate prefix (``sum_``, ``avg_``, etc.) or equal
    ``count``.

    ORDER BY is flat: both dimension column names and aggregate field names
    (``count``, ``sum_Total``, etc.) are valid keys at the same level.
    """
    sa_table = _table_from_def(tdef)
    aliased = sa_table.alias("_grp")

    resolved_policy: "ResolvedPolicy | None" = None
    if resolve_policy is not None:
        resolved_policy = resolve_policy(tdef.name)

    requested = _collect_field_names(field_nodes)
    dim_col_names = {c.name for c in tdef.columns if not c.is_array}

    # Dimensions are real table columns; aggregate fields (``count``,
    # ``sum_*``, …) are guaranteed not to collide by the boot-time guard
    # in ``create_graphql_subapp``. So a requested field is a dimension
    # iff it's in ``dim_col_names``.
    dimension_fields = [f for f in requested if f in dim_col_names]

    group_cols = [aliased.c[d] for d in dimension_fields]
    agg_projections: list = []

    if COUNT_FIELD in requested:
        agg_projections.append(func.count().label(COUNT_FIELD))

    col_map = {c.name: c for c in tdef.columns}

    for fname in sorted(requested):
        if fname == COUNT_FIELD or fname in dim_col_names:
            continue
        for prefix in _AGG_PREFIXES:
            if fname.startswith(prefix):
                col_name = fname[len(prefix) :]
                col_def = col_map.get(col_name)
                if col_def is None or col_def.is_array:
                    break
                if (
                    resolved_policy is not None
                    and not resolved_policy.is_column_allowed(col_name)
                ):
                    raise ColumnAccessDenied(tdef.name, [col_name])
                agg_fn_name = prefix.strip("_")
                agg_fn = _AGG_FUNC_MAP.get(agg_fn_name)
                if agg_fn is not None:
                    agg_projections.append(agg_fn(aliased.c[col_name]).label(fname))
                break

    stmt = select(*group_cols, *agg_projections).select_from(aliased)
    if group_cols:
        stmt = stmt.group_by(*group_cols)

    if resolved_policy is not None and resolved_policy.row_filter_clause is not None:
        stmt = stmt.where(resolved_policy.row_filter_clause)

    if where:
        stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

    if order_by:
        for item in order_by:
            for key, direction in item.items():
                order_fn = _ORDER_BY_MAP.get(direction)
                if order_fn is None:
                    raise ValueError(f"Unknown order_by direction: {direction!r}")
                if key in dim_col_names:
                    if (
                        resolved_policy is not None
                        and not resolved_policy.is_column_allowed(key)
                    ):
                        raise ColumnAccessDenied(tdef.name, [key])
                    stmt = stmt.order_by(order_fn(aliased.c[key]))
                else:
                    # aggregate field (count, sum_Total, etc.) — use literal label
                    stmt = stmt.order_by(order_fn(literal_column(key)))

    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)

    return stmt
