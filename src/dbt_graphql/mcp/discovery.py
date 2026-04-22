"""Schema discovery for MCP tools.

Provides static discovery (from ProjectInfo) and optional live enrichment
(from a live database connection).
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


class SchemaDiscovery:
    """Discover schema structure from a ProjectInfo IR."""

    def __init__(self, project, db=None) -> None:
        self._project = project
        self._db = db
        # Build adjacency for BFS path-finding
        self._adj: dict[
            str, list[tuple[str, str, str]]
        ] = {}  # table → [(via_col, to_table, to_col)]
        for rel in project.relationships:
            from_col = rel.from_columns[0] if rel.from_columns else ""
            to_col = rel.to_columns[0] if rel.to_columns else ""
            self._adj.setdefault(rel.from_model, []).append(
                (from_col, rel.to_model, to_col)
            )
            self._adj.setdefault(rel.to_model, []).append(
                (to_col, rel.from_model, from_col)
            )

    def list_tables(self) -> list[TableSummary]:
        return [
            TableSummary(
                name=m.name,
                description=m.description,
                column_count=len(m.columns),
                relationship_count=len(m.relationships),
            )
            for m in self._project.models
        ]

    def describe_table(self, name: str) -> TableDetail | None:
        model = next((m for m in self._project.models if m.name == name), None)
        if model is None:
            return None
        columns = [
            ColumnDetail(
                name=c.name,
                sql_type=c.type,
                not_null=c.not_null,
                is_unique=c.unique,
                description=c.description,
                enum_values=c.enum_values,
            )
            for c in model.columns
        ]
        relationships = [
            f"{rel.from_model}.{rel.from_columns[0] if rel.from_columns else ''} → "
            f"{rel.to_model}.{rel.to_columns[0] if rel.to_columns else ''}"
            for rel in model.relationships
        ]
        return TableDetail(
            name=name,
            description=model.description,
            columns=columns,
            relationships=relationships,
        )

    def find_path(self, from_table: str, to_table: str) -> list[JoinPath]:
        """BFS to find all shortest join paths between two tables.

        Processes nodes level-by-level so that multiple shortest paths through
        shared intermediate nodes are all returned, not just the first found.
        """
        if from_table == to_table:
            return [JoinPath()]

        # current_level: node → all partial paths (as step lists) that reach it
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
        for rel in self._project.relationships:
            if rel.from_model == table_name:
                result.append(
                    RelatedTable(
                        name=rel.to_model,
                        via_column=rel.from_columns[0] if rel.from_columns else "",
                        direction="outgoing",
                    )
                )
            elif rel.to_model == table_name:
                result.append(
                    RelatedTable(
                        name=rel.from_model,
                        via_column=rel.to_columns[0] if rel.to_columns else "",
                        direction="incoming",
                    )
                )
        return result

    # ---- Live enrichment (requires db connection) ----

    @staticmethod
    def _qi(name: str) -> str:
        """Double-quote an identifier, escaping any embedded double-quotes."""
        return '"' + name.replace('"', '""') + '"'

    async def get_row_count(self, table: str) -> int | None:
        if self._db is None:
            return None
        qt = self._qi(table)
        rows = await self._db.execute_text(f"SELECT COUNT(*) AS cnt FROM {qt}")
        return rows[0]["cnt"] if rows else None

    async def get_distinct_values(
        self, table: str, column: str, limit: int = 50
    ) -> list:
        if self._db is None:
            return []
        qt, qc = self._qi(table), self._qi(column)
        rows = await self._db.execute_text(
            f"SELECT DISTINCT {qc} FROM {qt} LIMIT {limit}"
        )
        return [r[column] for r in rows]

    async def get_date_range(
        self, table: str, column: str
    ) -> tuple[str | None, str | None]:
        if self._db is None:
            return None, None
        qt, qc = self._qi(table), self._qi(column)
        rows = await self._db.execute_text(
            f"SELECT MIN({qc}) AS mn, MAX({qc}) AS mx FROM {qt}"
        )
        if not rows:
            return None, None
        return str(rows[0]["mn"]), str(rows[0]["mx"])

    async def get_sample_rows(self, table: str, limit: int = 5) -> list[dict]:
        if self._db is None:
            return []
        qt = self._qi(table)
        return await self._db.execute_text(f"SELECT * FROM {qt} LIMIT {limit}")
