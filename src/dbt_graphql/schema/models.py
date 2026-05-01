"""Data classes for parsed GraphQL schema types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RelationDef:
    target_model: str
    target_column: str  # single-column alias (first of to_columns)
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)
    origin: str = ""
    cardinality: str = ""


@dataclass
class ColumnLineageRef:
    """A single upstream column edge feeding into a column."""

    source: str  # upstream model name
    column: str  # upstream column name
    type: str  # pass_through | rename | transformation


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
    lineage: list[ColumnLineageRef] = field(default_factory=list)


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
    lineage_sources: list[str] = field(default_factory=list)


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
