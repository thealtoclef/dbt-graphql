"""Format dbt project info as GraphQL db schema.

Produces:
- db.graphql: GraphQL SDL schema used by the query compiler.

Column types are mapped to standard GraphQL scalars (Int, Float, Boolean, String).
The exact SQL type is always preserved in an ``@column(type: "...")`` directive so the
compiler never needs to parse the GraphQL type name back into SQL.

Flow:
  ProjectInfo  ──build_registry()──▶  TableRegistry  (Python object, used by the server)
                                            │
                                  build_source_doc()  →  DocumentNode
                                            │
                                       render_sdl()
                                            │
                                            ▼
                                      db.graphql file  (--output mode)

The same ``render_sdl`` is used by the GraphQL ``_sdl`` field and the MCP
``describe_table`` tool, so file output and live SDL are byte-identical.
"""

from __future__ import annotations

import re

from graphql import DocumentNode, parse
from pydantic import BaseModel

from ...ir.models import ProjectInfo, RelationshipInfo
from ...schema.models import (
    ColumnDef,
    ColumnLineageRef,
    RelationDef,
    TableDef,
    TableRegistry,
)
from .view import render_sdl

# Explicit int aliases that don't end with "INT" (e.g. INTEGER, UINTEGER, INT64).
_INT_EXACT = frozenset({"INT", "INT2", "INT4", "INT8", "INT64", "INTEGER", "UINTEGER"})
# Float family: checked via startswith so FLOAT64, BIGNUMERIC, etc. are covered.
_FLOAT_PREFIXES = (
    "FLOAT",
    "DOUBLE",
    "REAL",
    "NUMERIC",
    "DECIMAL",
    "MONEY",
    "NUMBER",
    "BIGNUMERIC",
)
_BOOL_EXACT = frozenset({"BOOL", "BOOLEAN", "BIT"})


def _sql_to_gql_scalar(base: str) -> str:
    """Map a SQL base type to a standard GraphQL scalar. Unknown types map to String.

    Strategy (order matters):
    - Boolean: small closed set (BOOL, BOOLEAN, BIT).
    - Int: explicit aliases (INT, INTEGER, INT64 …) OR anything ending in "INT"
      (BIGINT, SMALLINT, TINYINT, HUGEINT, UBIGINT …). The endswith avoids
      enumerating every vendor variant while safely excluding INTERVAL (ends in AL).
    - Float: startswith covers FLOAT64, BIGNUMERIC, DOUBLEPRECISION, SMALLMONEY, etc.
    - Everything else: String.
    """
    upper = base.upper().replace(" ", "")
    if upper in _BOOL_EXACT:
        return "Boolean"
    if upper in _INT_EXACT or upper.endswith("INT"):
        return "Int"
    if upper.startswith(_FLOAT_PREFIXES):
        return "Float"
    return "String"


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


class GraphQLResult(BaseModel):
    """GraphQL schema output."""

    db_graphql: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_graphql(project: ProjectInfo) -> GraphQLResult:
    """Convert domain-neutral ProjectInfo into GraphQL db schema."""
    registry = build_registry(project)
    return GraphQLResult(db_graphql=render_sdl(build_source_doc(registry)))


def build_registry(project: ProjectInfo) -> TableRegistry:
    """Build a TableRegistry from dbt project info."""
    rel_map = _build_rel_map(project.relationships)
    table_sources_by_target: dict[str, list[str]] = {}
    for edge in project.table_lineage:
        table_sources_by_target.setdefault(edge.target, []).append(edge.source)

    # (target_model, target_column) -> [(source_model, source_column, lineage_type), ...]
    col_lineage_by_target: dict[tuple[str, str], list[ColumnLineageRef]] = {}
    for cedge in project.column_lineage:
        for col in cedge.columns:
            if not col.target_column:
                continue
            key = (cedge.target, col.target_column)
            col_lineage_by_target.setdefault(key, []).append(
                ColumnLineageRef(
                    source=cedge.source,
                    column=col.source_column,
                    type=str(col.lineage_type),
                )
            )

    tables: list[TableDef] = []

    for model in project.models:
        table = TableDef(
            name=model.name,
            database=model.database,
            schema=model.schema_,
            table=model.relation_name,
            description=model.description,
            lineage_sources=table_sources_by_target.get(model.name, []),
        )
        for col in model.columns:
            base, size, is_array = _parse_sql_type(col.type)
            scalar = _sql_to_gql_scalar(base)
            is_sole_pk = len(model.primary_keys) == 1 and col.name in model.primary_keys
            is_pk = is_sole_pk or col.is_primary_key
            is_unique = col.unique and not is_sole_pk and not col.is_primary_key

            col_def = ColumnDef(
                name=col.name,
                gql_type=scalar,
                is_array=is_array,
                not_null=col.not_null,
                is_pk=is_pk,
                is_unique=is_unique,
                sql_type=base,
                sql_size=size,
                description=col.description,
            )
            col_def.lineage = col_lineage_by_target.get((model.name, col.name), [])
            rel = rel_map.get((model.name, (col.name,)))
            if rel:
                single = len(rel.to_columns) == 1
                col_def.relation = RelationDef(
                    target_model=rel.to_model,
                    target_column=rel.to_columns[0] if single else "",
                    from_columns=[] if single else rel.from_columns,
                    to_columns=rel.to_columns,
                    origin=str(rel.origin),
                    cardinality=str(rel.cardinality),
                )
            table.columns.append(col_def)

        tables.append(table)

    return TableRegistry(tables)


# ---------------------------------------------------------------------------
# SDL serialisation  (TableRegistry → db.graphql string)
# ---------------------------------------------------------------------------


def _registry_to_sdl(registry: TableRegistry) -> str:
    """Serialise a TableRegistry to db.graphql SDL format."""
    blocks = [_table_to_sdl(t) for t in registry]
    return "\n".join(blocks).rstrip() + "\n"


def build_source_doc(registry: TableRegistry) -> DocumentNode:
    """Render the registry to db.graphql SDL and parse it into an AST.

    The returned ``DocumentNode`` carries every custom directive
    (``@table``, ``@column``, ``@id``, ``@relation``, ``@masked``, ``@filtered``)
    and every description string verbatim.
    """
    return parse(_registry_to_sdl(registry))


def _description_block(text: str, indent: str = "") -> str:
    (
        """Render a dbt description as a GraphQL triple-quoted block.

    Returns "" when the description is empty so callers can prepend
    unconditionally without producing blank `"""
        """` blocks.
    """
    )
    stripped = text.strip() if text else ""
    if not stripped:
        return ""
    # Escape any embedded triple quotes so the block stays well-formed.
    safe = stripped.replace('"""', '\\"""')
    return f'{indent}"""\n{indent}{safe}\n{indent}"""\n'


def _gql_field_type(col: ColumnDef) -> str:
    """Return the GraphQL type for a column.

    PK columns keep their underlying scalar so ``{T}_bool_exp`` can
    dispatch the right ``_*_comparison_exp`` (numeric ops for int PKs,
    string ops for text/UUID PKs). The PK signal travels via the
    ``@id`` directive in the printed db.graphql artefact and ``_sdl``.
    """
    base = col.gql_type
    if col.is_array:
        base = f"[{base}]"
    if col.not_null:
        base += "!"
    return base


def _table_to_sdl(table: TableDef) -> str:
    directives = [
        f'@table(database: "{table.database}", schema: "{table.schema}",'
        f' name: "{table.table}")'
    ]
    if table.filtered:
        directives.append("@filtered")
    if table.lineage_sources:
        srcs = ", ".join(f'"{s}"' for s in table.lineage_sources)
        directives.append(f"@lineage(sources: [{srcs}])")
    header = f"type {table.name} {' '.join(directives)} {{"

    out = _description_block(table.description)
    out += header + "\n"
    for col in table.columns:
        col_desc = _description_block(col.description, indent="  ")
        if col_desc:
            out += col_desc
        out += "  " + _column_to_sdl(col) + "\n"
    out += "}"
    return out


def _column_to_sdl(col: ColumnDef) -> str:
    gql_type = _gql_field_type(col)

    sql_args = f'type: "{col.sql_type}"'
    if col.sql_size:
        sql_args += f', size: "{col.sql_size}"'

    directives: list[str] = [f"@column({sql_args})"]
    if col.is_pk:
        directives.append("@id")
    if col.is_unique:
        directives.append("@unique")
    if col.masked:
        directives.append("@masked")

    for ref in col.lineage:
        directives.append(
            f'@lineage(source: "{ref.source}", column: "{ref.column}",'
            f" type: {ref.type})"
        )

    if col.relation:
        r = col.relation
        if r.to_columns and len(r.to_columns) > 1:
            args = [
                f"type: {r.target_model}",
                f"fromField: [{', '.join(r.from_columns)}]",
                f"toField: [{', '.join(r.to_columns)}]",
            ]
        else:
            args = [
                f"type: {r.target_model}",
                f"fromField: {col.name}",
                f"toField: {r.target_column}",
            ]
        args.append(f"cardinality: {r.cardinality}")
        args.append(f"origin: {r.origin}")
        directives.append(f"@relation({', '.join(args)})")

    line = f"{col.name}: {gql_type}"
    dir_str = " ".join(directives)
    if dir_str:
        line += f" {dir_str}"
    return line


# ---------------------------------------------------------------------------
# Type parsing
# ---------------------------------------------------------------------------


_SIZE_RE = re.compile(r"^(.*?)\s*\((.+)\)\s*$")


def _parse_sql_type(raw: str) -> tuple[str, str, bool]:
    """Return (base_type, size, is_array) from a raw SQL type.

    >>> _parse_sql_type("VARCHAR(255)")
    ('VARCHAR', '255', False)
    >>> _parse_sql_type("NUMERIC(10,2)")
    ('NUMERIC', '10,2', False)
    >>> _parse_sql_type("INTEGER[]")
    ('INTEGER', '', True)
    >>> _parse_sql_type("ARRAY<STRING>")
    ('STRING', '', True)
    >>> _parse_sql_type("DOUBLE PRECISION")
    ('DOUBLE PRECISION', '', False)
    """
    s = raw.strip()
    is_array = False

    # BigQuery ARRAY<T>
    upper = s.upper()
    if upper.startswith("ARRAY<") and s.endswith(">"):
        s = s[6:-1].strip()
        is_array = True

    # Postgres T[]
    if s.endswith("[]"):
        is_array = True
        s = s[:-2].strip()

    m = _SIZE_RE.match(s)
    if m:
        return m.group(1).strip(), m.group(2).strip(), is_array

    return s.strip(), "", is_array


# ---------------------------------------------------------------------------
# Relationship map
# ---------------------------------------------------------------------------


def _build_rel_map(
    relationships: list[RelationshipInfo],
) -> dict[tuple[str, tuple[str, ...]], RelationshipInfo]:
    rel_map: dict[tuple[str, tuple[str, ...]], RelationshipInfo] = {}
    for rel in relationships:
        if not rel.from_columns or not rel.to_columns:
            continue
        key = (rel.from_model, tuple(rel.from_columns))
        rel_map[key] = rel
    return rel_map
