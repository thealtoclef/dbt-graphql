"""Parse db.graphql SDL into a typed registry.

Reads the ``db.graphql`` file produced by ``format_graphql`` and extracts
table definitions, column metadata, and relationships into plain Python dataclasses
consumed by the SQL compiler and resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from graphql import (
    DirectiveNode,
    DocumentNode,
    EnumValueNode,
    FieldDefinitionNode,
    ListTypeNode,
    ListValueNode,
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
    target_column: str  # single-column alias (first of to_columns)
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)
    origin: str = ""
    confidence: str = ""


@dataclass
class ColumnDef:
    name: str
    gql_type: str  # Standard GraphQL scalar (Int, Float, Boolean, String)
    is_array: bool = False
    not_null: bool = False
    is_pk: bool = False
    is_unique: bool = False
    sql_type: str = ""  # raw SQL type from @sql directive
    sql_size: str = ""  # size/precision from @sql directive
    relation: RelationDef | None = None
    description: str = ""
    # Set per-request by the policy filter when a column-level mask
    # applies to the caller. Drives the @masked SDL directive.
    masked: bool = False


@dataclass
class TableDef:
    name: str
    database: str = ""
    schema: str = ""
    table: str = ""  # physical table name (may differ from GraphQL type name)
    columns: list[ColumnDef] = field(default_factory=list)
    description: str = ""
    # Set per-request by the policy filter when a row filter applies to
    # the caller. Drives the @filtered SDL directive.
    filtered: bool = False


@dataclass
class SchemaInfo:
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
# Directive helpers
# ---------------------------------------------------------------------------


def _directive_args(directive: DirectiveNode) -> dict[str, str | list[str]]:
    """Flatten a directive's keyword arguments into a plain dict."""
    out: dict[str, str | list[str]] = {}
    for arg in directive.arguments or []:
        val = arg.value
        if isinstance(val, ListValueNode):
            items: list[str] = []
            for v in val.values:
                if isinstance(v, StringValueNode):
                    items.append(v.value)
                elif isinstance(v, EnumValueNode):
                    items.append(v.value)
            out[arg.name.value] = items
        else:
            out[arg.name.value] = val.value  # type: ignore[ty:unresolved-attribute]
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
    description = field_node.description.value if field_node.description else ""

    col = ColumnDef(
        name=field_node.name.value,
        gql_type=gql_type,
        is_array=is_array,
        not_null=not_null,
        description=description,
    )

    # PK signal carried by the built-in ID scalar. Parser preserves
    # gql_type as-is for the SDL round-trip; ``is_pk`` is the canonical
    # flag downstream consumers read.
    if gql_type == "ID":
        col.is_pk = True

    for directive in field_node.directives or []:
        dname = directive.name.value
        if dname == "unique":
            col.is_unique = True
        elif dname == "masked":
            col.masked = True
        elif dname == "column":
            args = _directive_args(directive)
            col.sql_type = str(args.get("type", ""))
            col.sql_size = str(args.get("size", ""))
        elif dname == "relation":
            args = _directive_args(directive)
            target_col = args.get("field", "")
            to_fields = args.get("toFields", [])
            from_fields = args.get("fields", [])
            # Normalize to list form
            if isinstance(from_fields, list):
                fc = from_fields
            elif from_fields:
                fc = [str(from_fields)]
            else:
                fc = []
            if isinstance(to_fields, list):
                tc = to_fields
            elif to_fields:
                tc = [str(to_fields)]
            elif target_col:
                tc = [str(target_col)]
            else:
                tc = []

            col.relation = RelationDef(
                target_model=str(args.get("type", "")),
                target_column=str(target_col),
                from_columns=fc,
                to_columns=tc,
                origin=str(args.get("origin", "")),
                confidence=str(args.get("confidence", "")),
            )

    return col


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_db_graphql(sdl: str) -> tuple[SchemaInfo, TableRegistry]:
    """Parse a ``db.graphql`` SDL string into ``SchemaInfo`` + ``TableRegistry``."""
    doc: DocumentNode = parse(sdl)

    tables: list[TableDef] = []
    for defn in doc.definitions:
        if not isinstance(defn, ObjectTypeDefinitionNode):
            continue

        table = TableDef(
            name=defn.name.value,
            description=defn.description.value if defn.description else "",
        )

        for directive in defn.directives or []:
            args = _directive_args(directive)
            dname = directive.name.value
            if dname == "table":
                table.database = str(args.get("database", ""))
                table.schema = str(args.get("schema", ""))
                table.table = str(args.get("name", ""))
            elif dname == "filtered":
                table.filtered = True

        if not table.table:
            table.table = table.name

        for field_node in defn.fields or []:
            table.columns.append(_parse_column(field_node))

        tables.append(table)

    info = SchemaInfo(tables=tables)
    return info, TableRegistry(tables)


def load_db_graphql(path: str | Path) -> tuple[SchemaInfo, TableRegistry]:
    """Load and parse a ``db.graphql`` file from disk."""
    return parse_db_graphql(Path(path).read_text())
