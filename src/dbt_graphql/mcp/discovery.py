"""Schema discovery for MCP tools.

Structure (tables, columns, types, FK relationships) is derived from the
GraphQL ``TableRegistry`` — i.e. *the same view the API serves*. The dbt
``ProjectInfo`` is optional and contributes *enrichment only*: human
descriptions and enum values that don't survive into the GraphQL SDL.

This is deliberate: MCP must not be able to surface a table or column
that GraphQL won't expose. The registry is the contract; project is
metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from dbt_graphql.config import EnrichmentConfig
from dbt_graphql.formatter.schema import TableDef, TableRegistry


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

    Live row counts, sample rows, and value summaries are layered on
    when a DB connection is provided. dbt descriptions and enum values
    are layered on when a ``ProjectInfo`` is provided.
    """

    def __init__(
        self,
        registry: TableRegistry,
        *,
        project=None,
        db=None,
        enrichment=None,
    ) -> None:
        self._registry = registry
        self._meta = _Enrichment(project)
        self._db = db
        self._enrichment = enrichment or EnrichmentConfig()
        self._cache: dict[str, TableDetail] = {}

        from sqlalchemy.dialects import registry as _dialect_reg

        self._preparer = (
            _dialect_reg.load(db.dialect_name)().identifier_preparer
            if db is not None
            else None
        )

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

    async def describe_table(self, name: str) -> TableDetail | None:
        """Return full column + enrichment detail for a table.

        Cached for the lifetime of this instance. Live enrichment runs
        only when a DB connection is provided.
        """
        tdef = self._registry.get(name)
        if tdef is None:
            return None

        if name in self._cache:
            return self._cache[name]

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

        detail = TableDetail(
            name=name,
            description=self._meta.table_descriptions.get(name, ""),
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

    # ---- live enrichment (only called when db is set) ----

    async def _enrich(self, detail: TableDetail) -> None:
        """Populate live fields on ``detail`` in-place."""
        assert self._db is not None
        assert self._preparer is not None

        cfg = self._enrichment
        qi = self._preparer.quote_identifier

        detail.row_count = await self._get_row_count(detail.name, qi)
        detail.sample_rows = await self._get_sample_rows(detail.name, qi, limit=3)

        remaining = [cfg.budget]

        async def _enrich_col(col: ColumnDetail) -> None:
            if col.enum_values is not None:
                return
            # Budget check-and-decrement is atomic in single-threaded
            # asyncio (no await between check and decrement).
            if remaining[0] <= 0:
                return
            remaining[0] -= 1

            if _is_date_type(col.sql_type):
                mn, mx = await self._get_date_range(detail.name, col.name, qi)
                if mn is not None:
                    col.value_summary = {"kind": "range", "min": mn, "max": mx}
            else:
                values = await self._get_distinct_values(
                    detail.name,
                    col.name,
                    qi,
                    limit=cfg.distinct_values_max_cardinality + 1,
                )
                if len(values) <= cfg.distinct_values_max_cardinality:
                    col.value_summary = {
                        "kind": "distinct",
                        "values": values[: cfg.distinct_values_limit],
                    }

        await asyncio.gather(*(_enrich_col(c) for c in detail.columns))

    async def _get_row_count(self, table: str, qi) -> int | None:
        qt = qi(table)
        rows = await self._db.execute_text(f"SELECT COUNT(*) AS cnt FROM {qt}")
        return rows[0]["cnt"] if rows else None

    async def _get_distinct_values(
        self, table: str, column: str, qi, limit: int = 50
    ) -> list:
        qt, qc = qi(table), qi(column)
        rows = await self._db.execute_text(
            f"SELECT DISTINCT {qc} FROM {qt} LIMIT {limit}"
        )
        return [next(iter(r.values())) for r in rows]

    async def _get_date_range(
        self, table: str, column: str, qi
    ) -> tuple[str | None, str | None]:
        qt, qc = qi(table), qi(column)
        rows = await self._db.execute_text(
            f"SELECT MIN({qc}) AS mn, MAX({qc}) AS mx FROM {qt}"
        )
        if not rows:
            return None, None
        return str(rows[0]["mn"]), str(rows[0]["mx"])

    async def _get_sample_rows(self, table: str, qi, limit: int = 5) -> list[dict]:
        qt = qi(table)
        return await self._db.execute_text(f"SELECT * FROM {qt} LIMIT {limit}")
