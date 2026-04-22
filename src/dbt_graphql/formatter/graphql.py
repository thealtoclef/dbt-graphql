"""Format dbt project info as GraphQL db schema.

Produces:
- db.graphql: GraphQL SDL schema used by the query compiler.

Column types are mapped to standard GraphQL scalars (Int, Float, Boolean, String).
The exact SQL type is always preserved in an ``@column(type: "...")`` directive so the
compiler never needs to parse the GraphQL type name back into SQL.
"""

from __future__ import annotations

import re
from ..ir.models import ColumnInfo, ProjectInfo, ModelInfo, RelationshipInfo
from pydantic import BaseModel

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
    return GraphQLResult(db_graphql=_build_db_graphql(project))


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
# db.graphql builder
# ---------------------------------------------------------------------------


def _build_db_graphql(project: ProjectInfo) -> str:
    """Build a GraphQL SDL schema for all dbt models."""
    rel_map = _build_rel_map(project.relationships)
    incoming_rels = _build_incoming_rels(project.relationships)
    blocks: list[str] = []
    for model in project.models:
        blocks.append(_type_block(model, rel_map, incoming_rels))
    return "\n".join(blocks).rstrip() + "\n"


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


def _build_incoming_rels(
    relationships: list[RelationshipInfo],
) -> dict[str, list[RelationshipInfo]]:
    """Map target model name to list of relationships pointing into it."""
    incoming: dict[str, list[RelationshipInfo]] = {}
    for rel in relationships:
        if not rel.from_columns or not rel.to_columns:
            continue
        incoming.setdefault(rel.to_model, []).append(rel)
    return incoming


def _type_block(
    model: ModelInfo,
    rel_map: dict[tuple[str, tuple[str, ...]], RelationshipInfo],
    incoming_rels: dict[str, list[RelationshipInfo]],
) -> str:
    """Build a GraphQL SDL type block for a dbt model."""
    type_directives: list[str] = [
        f'@table(database: "{model.database}", schema: "{model.schema_}", name: "{model.relation_name}")',
    ]

    header = f"type {model.name} " + " ".join(type_directives) + " {"

    lines = [header]
    existing_names: set[str] = set()
    for col in model.columns:
        lines.append("  " + _column_line(model, col, rel_map))
        existing_names.add(col.name)

    # Reverse-relation fields
    for rel in incoming_rels.get(model.name, []):
        rev_line = _reverse_relation_line(rel, existing_names)
        if rev_line:
            lines.append("  " + rev_line)

    lines.append("}")
    return "\n".join(lines)


def _reverse_relation_line(
    rel: RelationshipInfo, existing_names: set[str]
) -> str | None:
    """Emit a reverse-relation field on the target model."""
    field_name = rel.from_model + "s"  # simple plural
    if field_name in existing_names:
        field_name += "_rev"
    existing_names.add(field_name)
    return f'{field_name}: [{rel.from_model}] @reverseRelation(from: {rel.from_model}, via: "{rel.from_columns[0]}")'


def _column_line(
    model: ModelInfo,
    col: ColumnInfo,
    rel_map: dict[tuple[str, tuple[str, ...]], RelationshipInfo],
) -> str:
    base, size, is_array = _parse_sql_type(col.type)
    scalar = _sql_to_gql_scalar(base)
    gql_type = f"[{scalar}]" if is_array else scalar
    if col.not_null:
        gql_type += "!"

    sql_args = f'type: "{base}"'
    if size:
        sql_args += f', size: "{size}"'
    directives: list[str] = [f"@column({sql_args})"]
    is_sole_pk = len(model.primary_keys) == 1 and col.name in model.primary_keys
    if is_sole_pk or col.is_primary_key:
        directives.append("@id")
    if col.unique and not is_sole_pk and not col.is_primary_key:
        directives.append("@unique")

    # Look up by single-column key (most common path)
    rel = rel_map.get((model.name, (col.name,)))
    if rel:
        args = [f"type: {rel.to_model}"]
        if len(rel.to_columns) == 1:
            args.append(f"field: {rel.to_columns[0]}")
        else:
            args.append(f"fields: [{', '.join(rel.from_columns)}]")
            args.append(f"toFields: [{', '.join(rel.to_columns)}]")
        args.append(f"origin: {rel.origin}")
        args.append(f"confidence: {rel.cardinality_confidence}")
        if rel.business_name:
            args.append(f'name: "{rel.business_name}"')
        if rel.description:
            args.append(f'description: "{rel.description}"')
        directives.append(f"@relation({', '.join(args)})")

    dir_str = " ".join(directives)
    line = f"{col.name}: {gql_type}"
    if dir_str:
        line += f" {dir_str}"
    return line
