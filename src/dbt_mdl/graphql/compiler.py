"""Compile GraphQL field selections into SQLAlchemy Core queries.

Emits flat SELECT statements for single-table queries and correlated
subqueries for nested relations — no LATERAL joins, so this is safe for
Apache Doris.

Dialect-specific JSON functions are handled via SQLAlchemy's ``compiles``
extension so the query builder stays database-agnostic.
"""

from __future__ import annotations

from sqlalchemy import Column, Select, literal, select, table
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from .schema import ColumnDef, RelationDef, TableDef, TableRegistry


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
    """Create an ad-hoc SQLAlchemy ``TableClause`` for FROM clauses."""
    cols = [Column(c.name) for c in tdef.columns]
    return table(tdef.table, *cols)


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
) -> Select:
    """Build a correlated subquery for a nested relation.

    Uses SQLAlchemy expressions so dialect-specific compilation
    (JSON functions, quoting, etc.) is handled automatically.
    """
    child_scalars, _ = _extract_scalar_fields(target, child_fields, TableRegistry())
    child_table = _table_from_def(target).alias("child")

    # JSON_OBJECT('col', child.col, 'col2', child.col2, ...)
    json_args = []
    for col_name in child_scalars:
        json_args.append(literal(col_name))
        json_args.append(child_table.c[col_name])

    inner = json_build_obj(*json_args)
    agg = json_agg(inner)

    return (
        select(agg)
        .where(child_table.c[rel.target_column] == parent_aliased.c[parent_fk])
        .correlate(parent_aliased)
    )



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    limit: int | None = None,
    offset: int | None = None,
    where: dict[str, object] | None = None,
) -> Select:
    """Build a SQLAlchemy Core ``Select`` for a root GraphQL field.

    The returned ``Select`` is dialect-agnostic — compile it against a
    specific dialect (or execute via an engine) to get the right SQL.
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
        )
        cols.append(subquery.label(col.name))

    stmt = select(*cols).select_from(aliased)

    # WHERE
    if where:
        for col_name, value in where.items():
            if col_name in aliased.c:
                stmt = stmt.where(aliased.c[col_name] == value)

    # Pagination
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)

    return stmt
