"""Format dbt project info as GraphQL db schema.

Produces:
- db.graphql: GraphQL SDL schema used by the query compiler.

Scalar emission: the SDL type is the raw SQL column type pascal-cased per
whitespace-separated word. It roundtrips back through the query-compiler's
`pascalToSnakeSpace` to a lowercase SQL-style token.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from ..ir.models import ColumnInfo, ProjectInfo, ModelInfo, RelationshipInfo

# ---------------------------------------------------------------------------
# dbt adapter → database type.
# ---------------------------------------------------------------------------

_DB_TYPE_MAP: dict[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mariadb",
    "doris": "mysql",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "oracle": "oracle",
    "snowflake": "snowflake",
    "mssql": "mssql",
    "sqlserver": "mssql",
}


def _map_db_type(data_source: str) -> str | None:
    return _DB_TYPE_MAP.get(data_source.lower())


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
    conn_type = project.adapter_type
    gj_db = _map_db_type(conn_type)
    if gj_db is None:
        supported = sorted(_DB_TYPE_MAP)
        raise ValueError(
            f"Unsupported adapter type `{conn_type}`. "
            f"Supported adapters: {', '.join(supported)}"
        )
    return GraphQLResult(
        db_graphql=_build_db_graphql(project, gj_db),
    )


# ---------------------------------------------------------------------------
# Type parsing / pascal-case emission
# ---------------------------------------------------------------------------

# Matches "type(size)" where size may contain comma/digits/etc.
_TYPE_WITH_SIZE_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9 _]*?)\s*\((.+)\)\s*$")


def _parse_sql_type(raw: str) -> tuple[str, str, bool]:
    """Split a raw SQL type into (base, size, is_array).

    >>> _parse_sql_type("VARCHAR(255)")
    ('varchar', '255', False)
    >>> _parse_sql_type("NUMERIC(10,2)")
    ('numeric', '10,2', False)
    >>> _parse_sql_type("INTEGER[]")
    ('integer', '', True)
    >>> _parse_sql_type("TIMESTAMP WITH TIME ZONE")
    ('timestamp with time zone', '', False)
    >>> _parse_sql_type("ARRAY<STRING>")
    ('string', '', True)
    """
    if not raw:
        return "", "", False

    s = raw.strip()
    is_array = False

    # BigQuery ARRAY<T>
    upper = s.upper()
    if upper.startswith("ARRAY<") and s.endswith(">"):
        inner = s[6:-1].strip()
        base, size, _ = _parse_sql_type(inner)
        return base, size, True

    # Postgres T[]
    if s.endswith("[]"):
        is_array = True
        s = s[:-2].strip()

    m = _TYPE_WITH_SIZE_RE.match(s)
    if m:
        base = m.group(1).strip().lower()
        size = m.group(2).strip()
        return base, size, is_array

    return s.lower(), "", is_array


def _pascal(name: str) -> str:
    """Pascal-case each whitespace-separated word.

    Matches GraphJin's template helper so the emitted SDL roundtrips through
    `pascalToSnakeSpace` back to the original lowercase SQL token.

    >>> _pascal("bigint")
    'Bigint'
    >>> _pascal("big int")
    'BigInt'
    >>> _pascal("timestamp with time zone")
    'TimestampWithTimeZone'
    >>> _pascal("character varying")
    'CharacterVarying'
    """
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return ""
    return "".join(p[:1].upper() + p[1:].lower() for p in parts)


# A few well-known synonyms we normalize before pascaling, so the resulting
# SDL looks natural (and hits a case in GraphJin's DDL mapper).
_SQL_TYPE_ALIASES: dict[str, str] = {
    # Normalize to the space-separated forms GraphJin's DDL mapper accepts —
    # the pascal-cased output (e.g. "big int" → "BigInt") matches webshop
    # example conventions and roundtrips cleanly through pascalToSnakeSpace.
    "int": "integer",
    "int2": "small int",
    "int4": "integer",
    "int8": "big int",
    "bigint": "big int",
    "smallint": "small int",
    "bigserial": "big serial",
    "tinyint": "small int",
    "bool": "boolean",
    "float4": "real",
    "float8": "double precision",
    "float": "double precision",
    "double": "double precision",
    "string": "text",
    "nvarchar": "varchar",
    "nchar": "char",
    "ntext": "text",
    "datetime": "timestamp",
    "timestamptz": "timestamp with time zone",
    "timestamp without time zone": "timestamp",
    "int64": "big int",
    "float64": "double precision",
    "bignumeric": "numeric",
    "bytes": "bytea",
    "blob": "bytea",
    "bit": "boolean",
}


def _sql_to_gql_type(raw: str) -> tuple[str, str]:
    """Return (gql_type_name, size_args) for a raw SQL type string.

    Does not include the array wrapping or the `!` suffix — caller handles those.
    """
    base, size, _is_array = _parse_sql_type(raw)
    base = _SQL_TYPE_ALIASES.get(base, base)
    if not base:
        return "Text", size
    return _pascal(base), size


# ---------------------------------------------------------------------------
# db.graphql builder
# ---------------------------------------------------------------------------


def _build_db_graphql(project: ProjectInfo, gj_db: str) -> str:
    """Build the GraphJin SDL schema (db.graphql).

    Generates a complete SDL schema for all dbt models with their types,
    columns, and @database/@schema/@table directives. This is the primary schema
    that GraphJin uses for query compilation.
    """
    schema_name = _default_schema(project)
    header = f"# dbinfo:{gj_db},,{schema_name}\n"

    rel_map = _build_rel_map(project.relationships)
    blocks: list[str] = [header]
    for model in project.models:
        blocks.append("")
        blocks.append(_type_block(model, rel_map))
    return "\n".join(blocks).rstrip() + "\n"


def _default_schema(project: ProjectInfo) -> str:
    for m in project.models:
        if m.schema_:
            return m.schema_
    return ""


def _build_rel_map(
    relationships: list[RelationshipInfo],
) -> dict[tuple[str, str], tuple[str, str]]:
    rel_map: dict[tuple[str, str], tuple[str, str]] = {}
    for rel in relationships:
        if not rel.from_column or not rel.to_column:
            continue
        rel_map[(rel.from_model, rel.from_column)] = (rel.to_model, rel.to_column)
    return rel_map


def _type_block(
    model: ModelInfo,
    rel_map: dict[tuple[str, str], tuple[str, str]],
) -> str:
    """Build a GraphJin SDL type block for a dbt model."""
    type_directives: list[str] = [
        f"@database(name: {model.database})",
        f"@schema(name: {model.schema_})",
        f"@table(name: {model.relation_name})",
    ]

    header = f"type {model.name}"
    if type_directives:
        header += " " + " ".join(type_directives)
    header += " {"

    lines = [header]
    for col in model.columns:
        lines.append("  " + _column_line(model, col, rel_map))
    lines.append("}")
    return "\n".join(lines)


def _column_line(
    model: ModelInfo,
    col: ColumnInfo,
    rel_map: dict[tuple[str, str], tuple[str, str]],
) -> str:
    gql_base, size = _sql_to_gql_type(col.type)
    _, _, is_array = _parse_sql_type(col.type)
    gql = f"[{gql_base}]" if is_array else gql_base
    if col.not_null:
        gql += "!"

    directives: list[str] = []
    if size:
        directives.append(f'@type(args: "{size}")')
    if col.is_primary_key or col.name == model.primary_key:
        directives.append("@id")
    if col.unique and not (col.is_primary_key or col.name == model.primary_key):
        directives.append("@unique")
    if col.is_hidden:
        directives.append("@blocked")

    rel = rel_map.get((model.name, col.name))
    if rel:
        target_model, target_col = rel
        directives.append(f"@relation(type: {target_model}, field: {target_col})")

    dir_str = " ".join(directives)
    line = f"{col.name}: {gql}"
    if dir_str:
        line += f" {dir_str}"
    return line
