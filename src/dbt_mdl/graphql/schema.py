"""Parse db.graphql SDL into a typed registry.

Reads the ``db.graphql`` file produced by ``format_graphql`` and extracts
table definitions, column metadata, and relationships into plain Python dataclasses
consumed by the SQL compiler and resolvers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from graphql import (
    DirectiveNode,
    DocumentNode,
    FieldDefinitionNode,
    ListTypeNode,
    NamedTypeNode,
    NonNullTypeNode,
    ObjectTypeDefinitionNode,
    StringValueNode,
    parse,
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RelationDef:
    target_model: str
    target_column: str


@dataclass
class ColumnDef:
    name: str
    gql_type: str  # e.g. "Integer", "Text", "Unknown"
    is_array: bool = False
    not_null: bool = False
    is_pk: bool = False
    is_unique: bool = False
    is_hidden: bool = False
    size_args: str = ""
    relation: RelationDef | None = None


@dataclass
class TableDef:
    name: str
    database: str = ""
    schema: str = ""
    table: str = ""  # physical table name (may differ from GraphQL type name)
    columns: list[ColumnDef] = field(default_factory=list)


@dataclass
class SchemaInfo:
    db_type: str = ""
    default_schema: str = ""
    tables: list[TableDef] = field(default_factory=list)


class TableRegistry:
    """Dict-like lookup of ``TableDef`` by name."""

    def __init__(self, tables: list[TableDef] | None = None) -> None:
        self._map: dict[str, TableDef] = {t.name: t for t in (tables or [])}

    def get(self, name: str) -> TableDef | None:
        return self._map.get(name)

    def __getitem__(self, name: str) -> TableDef:
        return self._map[name]

    def __contains__(self, name: str) -> bool:
        return name in self._map

    def __iter__(self):
        return iter(self._map.values())

    def __len__(self) -> int:
        return len(self._map)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

_DBINFO_RE = re.compile(r"^#\s*dbinfo:\s*([^,]*),([^,]*),(.+)$")


def _parse_header(sdl: str) -> tuple[str, str]:
    """Extract (db_type, default_schema) from the ``# dbinfo:`` comment."""
    for line in sdl.splitlines():
        m = _DBINFO_RE.match(line)
        if m:
            return m.group(1).strip(), m.group(3).strip()
    return "", ""


# ---------------------------------------------------------------------------
# Directive helpers
# ---------------------------------------------------------------------------


def _directive_args(directive: DirectiveNode) -> dict[str, str]:
    """Flatten a directive's keyword arguments into a plain dict."""
    out: dict[str, str] = {}
    for arg in directive.arguments or []:
        if isinstance(arg.value, StringValueNode):
            out[arg.name.value] = arg.value.value
        else:
            out[arg.name.value] = arg.value.value  # type: ignore[assignment]
    return out


def _unwrap_type(node) -> tuple[str, bool, bool]:
    """Return (type_name, not_null, is_array) from a field's type node."""
    not_null = False
    is_array = False

    inner = node
    if isinstance(inner, NonNullTypeNode):
        not_null = True
        inner = inner.type

    if isinstance(inner, ListTypeNode):
        is_array = True
        inner = inner.type
        if isinstance(inner, NonNullTypeNode):
            inner = inner.type

    if isinstance(inner, NamedTypeNode):
        return inner.name.value, not_null, is_array
    return "Unknown", not_null, is_array


def _parse_column(field_node: FieldDefinitionNode) -> ColumnDef:
    gql_type, not_null, is_array = _unwrap_type(field_node.type)

    col = ColumnDef(
        name=field_node.name.value,
        gql_type=gql_type,
        is_array=is_array,
        not_null=not_null,
    )

    for directive in field_node.directives or []:
        dname = directive.name.value
        if dname == "id":
            col.is_pk = True
        elif dname == "unique":
            col.is_unique = True
        elif dname == "blocked":
            col.is_hidden = True
        elif dname == "type":
            args = _directive_args(directive)
            col.size_args = args.get("args", "")
        elif dname == "relation":
            args = _directive_args(directive)
            col.relation = RelationDef(
                target_model=args.get("type", ""),
                target_column=args.get("field", ""),
            )

    return col


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_db_graphql(sdl: str) -> tuple[SchemaInfo, TableRegistry]:
    """Parse a ``db.graphql`` SDL string into ``SchemaInfo`` + ``TableRegistry``."""
    db_type, default_schema = _parse_header(sdl)
    doc: DocumentNode = parse(sdl)

    tables: list[TableDef] = []
    for defn in doc.definitions:
        if not isinstance(defn, ObjectTypeDefinitionNode):
            continue

        table = TableDef(name=defn.name.value)

        for directive in defn.directives or []:
            args = _directive_args(directive)
            dname = directive.name.value
            if dname == "database":
                table.database = args.get("name", "")
            elif dname == "schema":
                table.schema = args.get("name", "")
            elif dname == "table":
                table.table = args.get("name", "")

        if not table.table:
            table.table = table.name

        for field_node in defn.fields or []:
            table.columns.append(_parse_column(field_node))

        tables.append(table)

    info = SchemaInfo(
        db_type=db_type,
        default_schema=default_schema,
        tables=tables,
    )
    return info, TableRegistry(tables)


def load_db_graphql(path: str | Path) -> tuple[SchemaInfo, TableRegistry]:
    """Load and parse a ``db.graphql`` file from disk."""
    return parse_db_graphql(Path(path).read_text())
