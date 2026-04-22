"""Schema discovery for MCP tools.

Provides static discovery (from ProjectInfo) and optional live enrichment
(from a live database connection).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


def _is_date_type(sql_type: str) -> bool:
    """Return True for date/time SQL types across common adapters."""
    t = sql_type.lower().split("(")[0].strip()
    return t in {
        "date",
        "datetime",
        "time",
        "timestamp",
        "timestamptz",
        "timestamp with time zone",
        "timestamp without time zone",
    }


@dataclass
class ColumnDetail:
    name: str
    sql_type: str
    not_null: bool = False
    is_unique: bool = False
    description: str = ""
    enum_values: list[str] | None = None
    value_summary: dict | None = None


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
    row_count: int | None = None
    sample_rows: list[dict] = field(default_factory=list)


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

    def __init__(self, project, db=None, enrichment=None) -> None:
        self._project = project
        self._db = db
        self._enrichment = enrichment
        self._cache: dict[str, TableDetail] = {}

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

    async def describe_table(self, name: str) -> TableDetail | None:
        """Return full column + enrichment detail for a table.

        Results are cached for the lifetime of this SchemaDiscovery instance.
        Live enrichment runs only when a DB connection is provided.
        """
        model = next((m for m in self._project.models if m.name == name), None)
        if model is None:
            return None

        if name in self._cache:
            return self._cache[name]

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
        detail = TableDetail(
            name=name,
            description=model.description,
            columns=columns,
            relationships=relationships,
        )

        # Static enum summaries — no DB needed.
        for col in detail.columns:
            if col.enum_values is not None:
                col.value_summary = {"kind": "enum", "values": col.enum_values}

        if self._db is not None:
            await self._enrich(detail)

        self._cache[name] = detail
        return detail

    async def _enrich(self, detail: TableDetail) -> None:
        """Populate live fields on detail in-place: row_count, sample_rows, value_summary."""
        cfg = self._enrichment
        budget: int = cfg.budget if cfg else 20
        distinct_limit: int = cfg.distinct_values_limit if cfg else 50
        max_cardinality: int = cfg.distinct_values_max_cardinality if cfg else 500

        detail.row_count = await self.get_row_count(detail.name)
        detail.sample_rows = await self.get_sample_rows(detail.name, limit=3)

        remaining = [budget]

        async def _enrich_col(col: ColumnDetail) -> None:
            # Enums are already set statically; skip live query.
            if col.enum_values is not None:
                return
            # Budget check-and-decrement is atomic in single-threaded asyncio
            # (no await between check and decrement).
            if remaining[0] <= 0:
                return
            remaining[0] -= 1

            if _is_date_type(col.sql_type):
                mn, mx = await self.get_date_range(detail.name, col.name)
                if mn is not None:
                    col.value_summary = {"kind": "range", "min": mn, "max": mx}
            else:
                values = await self.get_distinct_values(
                    detail.name, col.name, limit=max_cardinality + 1
                )
                if len(values) <= max_cardinality:
                    col.value_summary = {
                        "kind": "distinct",
                        "values": values[:distinct_limit],
                    }

        await asyncio.gather(*(_enrich_col(c) for c in detail.columns))

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
