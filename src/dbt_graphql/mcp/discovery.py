"""Schema discovery for MCP tools.

Structure (tables, columns, types, FK relationships) is derived from the
GraphQL ``TableRegistry`` — i.e. *the same view the API serves*. The dbt
``ProjectInfo`` is optional and contributes *enrichment only*: human
descriptions and declared enum values that don't survive into the
GraphQL SDL.

This is deliberate: MCP must not be able to surface a table or column
that GraphQL won't expose. The registry is the contract; project is
metadata. Discovery is **manifest-only** — no live warehouse queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dbt_graphql.formatter.schema import TableDef, TableRegistry


@dataclass
class ColumnDetail:
    name: str
    sql_type: str
    not_null: bool = False
    is_unique: bool = False
    description: str = ""
    enum_values: list[str] | None = None


@dataclass
class TableSummary:
    name: str
    description: str = ""
    column_count: int = 0
    relationship_count: int = 0


@dataclass
class TableDetail:
    name: str
    description: str = ""
    columns: list[ColumnDetail] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)


@dataclass
class JoinStep:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class JoinPath:
    steps: list[JoinStep] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.steps)


@dataclass
class RelatedTable:
    name: str
    via_column: str
    direction: str  # "outgoing" | "incoming"


class _Enrichment:
    """Side-table of dbt-only metadata indexed by (table, column)."""

    def __init__(self, project) -> None:
        self.table_descriptions: dict[str, str] = {}
        self.column_descriptions: dict[tuple[str, str], str] = {}
        self.column_enums: dict[tuple[str, str], list[str]] = {}
        if project is None:
            return
        for m in project.models:
            self.table_descriptions[m.name] = m.description or ""
            for c in m.columns:
                key = (m.name, c.name)
                if c.description:
                    self.column_descriptions[key] = c.description
                if c.enum_values is not None:
                    self.column_enums[key] = c.enum_values


class SchemaDiscovery:
    """Discover schema structure from the GraphQL ``TableRegistry``.

    dbt descriptions and declared enum values are layered on when a
    ``ProjectInfo`` is provided. No live DB access — the manifest is
    the single source of truth.
    """

    def __init__(
        self,
        registry: TableRegistry,
        *,
        project=None,
    ) -> None:
        self._registry = registry
        self._meta = _Enrichment(project)

        # Build adjacency from registry — outgoing edges live on each
        # column's ``relation``; incoming edges are the reverse.
        self._adj: dict[str, list[tuple[str, str, str]]] = {}
        for tdef in self._registry:
            for col in tdef.columns:
                rel = col.relation
                if rel is None or not rel.target_model:
                    continue
                from_col = rel.from_columns[0] if rel.from_columns else col.name
                to_col = (
                    rel.to_columns[0] if rel.to_columns else rel.target_column or ""
                )
                self._adj.setdefault(tdef.name, []).append(
                    (from_col, rel.target_model, to_col)
                )
                self._adj.setdefault(rel.target_model, []).append(
                    (to_col, tdef.name, from_col)
                )

    # ---- structure (registry-driven) ----

    def list_tables(self) -> list[TableSummary]:
        return [
            TableSummary(
                name=t.name,
                description=self._meta.table_descriptions.get(t.name, ""),
                column_count=len(t.columns),
                relationship_count=sum(1 for c in t.columns if c.relation is not None),
            )
            for t in self._registry
        ]

    def describe_table(self, name: str) -> TableDetail | None:
        """Return full column detail for a table from the manifest."""
        tdef = self._registry.get(name)
        if tdef is None:
            return None

        columns = [
            ColumnDetail(
                name=c.name,
                sql_type=c.sql_type or c.gql_type,
                not_null=c.not_null,
                is_unique=c.is_unique or c.is_pk,
                description=self._meta.column_descriptions.get((name, c.name), ""),
                enum_values=self._meta.column_enums.get((name, c.name)),
            )
            for c in tdef.columns
            if c.relation is None  # skip relation pseudo-fields
        ]
        relationships = self._format_relationships(tdef)

        return TableDetail(
            name=name,
            description=self._meta.table_descriptions.get(name, ""),
            columns=columns,
            relationships=relationships,
        )

    def _format_relationships(self, tdef: TableDef) -> list[str]:
        out: list[str] = []
        for col in tdef.columns:
            rel = col.relation
            if rel is None or not rel.target_model:
                continue
            from_col = rel.from_columns[0] if rel.from_columns else col.name
            to_col = rel.to_columns[0] if rel.to_columns else rel.target_column or ""
            out.append(f"{tdef.name}.{from_col} → {rel.target_model}.{to_col}")
        return out

    def find_path(self, from_table: str, to_table: str) -> list[JoinPath]:
        """BFS to find all shortest join paths between two tables.

        Processes nodes level-by-level so multiple shortest paths
        through shared intermediate nodes are all returned.
        """
        if from_table == to_table:
            return [JoinPath()]

        current_level: dict[str, list[list[JoinStep]]] = {from_table: [[]]}
        visited: set[str] = {from_table}
        shortest: list[JoinPath] = []

        while current_level and not shortest:
            next_level: dict[str, list[list[JoinStep]]] = {}
            for current, paths in current_level.items():
                for via_col, neighbor, neighbor_col in self._adj.get(current, []):
                    step = JoinStep(
                        from_table=current,
                        from_column=via_col,
                        to_table=neighbor,
                        to_column=neighbor_col,
                    )
                    for path in paths:
                        new_path = path + [step]
                        if neighbor == to_table:
                            shortest.append(JoinPath(steps=new_path))
                        elif neighbor not in visited:
                            next_level.setdefault(neighbor, []).append(new_path)

            visited.update(next_level.keys())
            current_level = next_level

        return shortest

    def explore_relationships(self, table_name: str) -> list[RelatedTable]:
        """Return all tables directly related to the given table."""
        result: list[RelatedTable] = []
        # outgoing — from this table's columns
        tdef = self._registry.get(table_name)
        if tdef is not None:
            for col in tdef.columns:
                rel = col.relation
                if rel is None or not rel.target_model:
                    continue
                from_col = rel.from_columns[0] if rel.from_columns else col.name
                result.append(
                    RelatedTable(
                        name=rel.target_model,
                        via_column=from_col,
                        direction="outgoing",
                    )
                )
        # incoming — scan every other table for relations pointing here
        for other in self._registry:
            if other.name == table_name:
                continue
            for col in other.columns:
                rel = col.relation
                if rel is None or rel.target_model != table_name:
                    continue
                to_col = (
                    rel.to_columns[0] if rel.to_columns else rel.target_column or ""
                )
                result.append(
                    RelatedTable(
                        name=other.name,
                        via_column=to_col,
                        direction="incoming",
                    )
                )
        return result
