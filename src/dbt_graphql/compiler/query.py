"""Compile GraphQL field selections into SQLAlchemy Core queries.

Emits flat SELECT statements for single-table queries and correlated
subqueries for nested relations — no LATERAL joins, so this is safe for
Apache Doris.

Dialect-specific JSON functions are handled via SQLAlchemy's ``compiles``
extension so the query builder stays database-agnostic.
"""

from __future__ import annotations

from sqlalchemy import Column, Select, and_, literal, select, table
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from ..formatter.schema import ColumnDef, RelationDef, TableDef, TableRegistry


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


@compiles(json_agg, "sqlite")
def _sqlite_json_agg(element, compiler, **kw):
    return "JSON_GROUP_ARRAY(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_agg, "duckdb")
def _duckdb_json_agg(element, compiler, **kw):
    return "LIST(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_agg, "postgresql")
def _pg_json_agg(element, compiler, **kw):
    return "JSONB_AGG(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_agg)
def _default_json_agg(element, compiler, **kw):
    return "JSON_ARRAYAGG(%s)" % compiler.process(element.clauses, **kw)


# --- json_build_obj per-dialect compilers ---


@compiles(json_build_obj, "mysql")
@compiles(json_build_obj, "mariadb")
@compiles(json_build_obj, "sqlite")
@compiles(json_build_obj, "duckdb")
def _standard_json_object(element, compiler, **kw):
    return "JSON_OBJECT(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_build_obj, "postgresql")
def _pg_json_build_obj(element, compiler, **kw):
    return "JSONB_BUILD_OBJECT(%s)" % compiler.process(element.clauses, **kw)


@compiles(json_build_obj)
def _default_json_build_obj(element, compiler, **kw):
    return "JSON_OBJECT(%s)" % compiler.process(element.clauses, **kw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _table_from_def(tdef: TableDef):
    cols = [Column(c.name) for c in tdef.columns]
    return table(tdef.table, *cols, schema=tdef.schema or None)


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
) -> Select:
    """Build a correlated subquery for a nested relation, recursively.

    Uses SQLAlchemy expressions so dialect-specific compilation
    (JSON functions, quoting, etc.) is handled automatically.
    Aliases each level as child_1, child_2, … to avoid name collisions.
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
    child_table = _table_from_def(target).alias(f"child_{depth}")
    new_visited = visited | {target.name}

    # JSON_OBJECT('col', child_N.col, ...)
    json_args = []
    for col_name in child_scalars:
        json_args.append(literal(col_name))
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

    return select(agg).where(predicate).correlate(parent_aliased)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    dialect: str = "",
    limit: int | None = None,
    offset: int | None = None,
    where: dict[str, object] | None = None,
    max_depth: int | None = None,
) -> Select:
    """Build a SQLAlchemy Core ``Select`` for a root GraphQL field.

    The returned ``Select`` is dialect-agnostic — compile it against a
    specific dialect (or execute via an engine) to get the right SQL.

    ``max_depth`` caps relation nesting (None = unlimited). Cycles in the
    query selection are always rejected regardless of this setting.
    """
    selection = field_nodes[0] if field_nodes else None
    if selection is None:
        return select()

    sub_fields = selection.selection_set.selections if selection.selection_set else []
    scalars, relations = _extract_scalar_fields(tdef, sub_fields, registry)

    sa_table = _table_from_def(tdef)
    parent_alias = "_parent"
    aliased = sa_table.alias(parent_alias)

    # Scalar columns
    cols = [aliased.c[name].label(name) for name in scalars]

    # Correlated subqueries for relations
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
        )
        cols.append(subquery.label(col.name))

    stmt = select(*cols).select_from(aliased)

    # WHERE
    if where:
        unknown = [k for k in where if k not in aliased.c]
        if unknown:
            raise ValueError(f"Unknown where column(s): {', '.join(sorted(unknown))}")
        for col_name, value in where.items():
            stmt = stmt.where(aliased.c[col_name] == value)

    # Pagination
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)

    return stmt
