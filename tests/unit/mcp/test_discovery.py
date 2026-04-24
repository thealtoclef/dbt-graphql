"""Tests for SchemaDiscovery (no live DB required)."""

import asyncio
from pathlib import Path

from dbt_graphql.pipeline import extract_project
from dbt_graphql.mcp.discovery import SchemaDiscovery
from dbt_graphql.ir.models import (
    ColumnInfo,
    JoinType,
    ModelInfo,
    ProjectInfo,
    RelationshipInfo,
    RelationshipOrigin,
)


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


def _make_discovery():
    project = extract_project(CATALOG, MANIFEST)
    return SchemaDiscovery(project)


class TestListTables:
    def test_returns_all_tables(self):
        d = _make_discovery()
        tables = d.list_tables()
        names = {t.name for t in tables}
        assert "customers" in names
        assert "orders" in names

    def test_table_has_column_count(self):
        d = _make_discovery()
        tables = d.list_tables()
        customers = next(t for t in tables if t.name == "customers")
        assert customers.column_count > 0


class TestDescribeTable:
    def test_returns_columns(self):
        d = _make_discovery()
        detail = asyncio.run(d.describe_table("customers"))
        assert detail is not None
        col_names = {c.name for c in detail.columns}
        assert "customer_id" in col_names

    def test_missing_table_returns_none(self):
        d = _make_discovery()
        assert asyncio.run(d.describe_table("nonexistent")) is None

    def test_no_db_returns_null_enrichment(self):
        d = _make_discovery()
        detail = asyncio.run(d.describe_table("customers"))
        assert detail is not None
        assert detail.row_count is None
        assert detail.sample_rows == []
        for col in detail.columns:
            if col.enum_values is None:
                assert col.value_summary is None

    def test_enum_column_gets_value_summary_without_db(self):
        col = ColumnInfo(
            name="status", type="VARCHAR", enum_values=["placed", "shipped"]
        )
        model = ModelInfo(name="orders", database="db", schema="main", columns=[col])
        project = ProjectInfo(
            project_name="test", adapter_type="postgres", models=[model]
        )
        d = SchemaDiscovery(project)
        detail = asyncio.run(d.describe_table("orders"))
        assert detail is not None
        status_col = next(c for c in detail.columns if c.name == "status")
        assert status_col.value_summary == {
            "kind": "enum",
            "values": ["placed", "shipped"],
        }

    def test_result_is_cached(self):
        d = _make_discovery()
        detail1 = asyncio.run(d.describe_table("customers"))
        detail2 = asyncio.run(d.describe_table("customers"))
        assert detail1 is detail2


class TestFindPath:
    def test_finds_direct_path(self):
        d = _make_discovery()
        paths = d.find_path("orders", "customers")
        assert len(paths) > 0
        assert paths[0].length == 1

    def test_same_table_returns_empty_path(self):
        d = _make_discovery()
        paths = d.find_path("orders", "orders")
        assert len(paths) == 1
        assert paths[0].length == 0

    def test_no_path_returns_empty(self):
        d = _make_discovery()
        paths = d.find_path("customers", "stg_orders")
        assert len(paths) == 0


class TestExploreRelationships:
    def test_orders_has_related_customers(self):
        d = _make_discovery()
        related = d.explore_relationships("orders")
        names = {r.name for r in related}
        assert "customers" in names

    def test_direction_outgoing(self):
        d = _make_discovery()
        related = d.explore_relationships("orders")
        customers_rel = next((r for r in related if r.name == "customers"), None)
        assert customers_rel is not None
        assert customers_rel.direction == "outgoing"


def _make_col(name: str) -> ColumnInfo:
    return ColumnInfo(name=name, type="INTEGER")


def _make_model(name: str) -> ModelInfo:
    return ModelInfo(
        name=name,
        database="db",
        schema="main",
        columns=[_make_col("id")],
    )


def _make_rel(
    from_model: str, from_col: str, to_model: str, to_col: str
) -> RelationshipInfo:
    return RelationshipInfo(
        name=f"{from_model}_{from_col}_{to_model}_{to_col}",
        from_model=from_model,
        from_columns=[from_col],
        to_model=to_model,
        to_columns=[to_col],
        join_type=JoinType.many_to_one,
        origin=RelationshipOrigin.data_test,
    )


class TestFindPathDiamond:
    """Verify that BFS returns all shortest paths, not just the first found.

    Diamond topology: A→B→D and A→C→D (both length 2 through distinct intermediates).
    """

    def _diamond_discovery(self) -> SchemaDiscovery:
        models = [_make_model(n) for n in ("A", "B", "C", "D")]
        # A→B, A→C, B→D, C→D
        rels = [
            _make_rel("A", "b_id", "B", "id"),
            _make_rel("A", "c_id", "C", "id"),
            _make_rel("B", "d_id", "D", "id"),
            _make_rel("C", "d_id", "D", "id"),
        ]
        project = ProjectInfo(
            project_name="test",
            adapter_type="postgres",
            models=models,
            relationships=rels,
        )
        return SchemaDiscovery(project)

    def test_returns_both_shortest_paths(self):
        d = self._diamond_discovery()
        paths = d.find_path("A", "D")
        assert len(paths) == 2, f"Expected 2 paths, got {len(paths)}: {paths}"
        assert all(p.length == 2 for p in paths)

    def test_intermediate_tables_differ(self):
        d = self._diamond_discovery()
        paths = d.find_path("A", "D")
        intermediates = {p.steps[0].to_table for p in paths}
        assert intermediates == {"B", "C"}

    def test_no_path_between_disconnected_tables_returns_empty(self):
        # Two disconnected clusters: {A, B} and {C, D} — no path from A to D.
        models = [_make_model(n) for n in ("A", "B", "C", "D")]
        rels = [_make_rel("A", "b_id", "B", "id"), _make_rel("C", "d_id", "D", "id")]
        project = ProjectInfo(
            project_name="test",
            adapter_type="postgres",
            models=models,
            relationships=rels,
        )
        d = SchemaDiscovery(project)
        assert d.find_path("A", "D") == []


class TestEnrichmentBudget:
    """Budget counter limits how many live DB queries are made per call."""

    def _make_mock_db(self, call_log: list):
        """Return a fake DB that records every call and returns minimal data."""

        class _MockDb:
            dialect_name = "postgresql"

            async def execute_text(self, sql: str):
                call_log.append(sql)
                if "COUNT(*)" in sql:
                    return [{"cnt": 0}]
                if "MIN(" in sql:
                    return [{"mn": None, "mx": None}]
                return []

        return _MockDb()

    def test_budget_zero_skips_column_enrichment(self):
        from dbt_graphql.config import EnrichmentConfig

        calls: list = []
        db = self._make_mock_db(calls)

        cols = [ColumnInfo(name=f"col{i}", type="VARCHAR") for i in range(5)]
        model = ModelInfo(name="t", database="db", schema="main", columns=cols)
        project = ProjectInfo(project_name="p", adapter_type="postgres", models=[model])
        enrichment = EnrichmentConfig(budget=0)
        d = SchemaDiscovery(project, db=db, enrichment=enrichment)
        detail = asyncio.run(d.describe_table("t"))
        assert detail is not None
        # row_count + sample_rows are NOT counted against budget
        assert detail.row_count == 0
        # No column queries should have fired
        col_queries = [q for q in calls if "DISTINCT" in q or "MIN(" in q]
        assert col_queries == []
        for col in detail.columns:
            assert col.value_summary is None

    def test_budget_limits_total_column_queries(self):
        from dbt_graphql.config import EnrichmentConfig

        calls: list = []
        db = self._make_mock_db(calls)

        cols = [ColumnInfo(name=f"col{i}", type="VARCHAR") for i in range(10)]
        model = ModelInfo(name="t", database="db", schema="main", columns=cols)
        project = ProjectInfo(project_name="p", adapter_type="postgres", models=[model])
        enrichment = EnrichmentConfig(budget=3)
        d = SchemaDiscovery(project, db=db, enrichment=enrichment)
        asyncio.run(d.describe_table("t"))
        col_queries = [q for q in calls if "DISTINCT" in q or "MIN(" in q]
        assert len(col_queries) == 3

    def test_cache_prevents_db_calls_on_second_call(self):
        calls: list = []
        db = self._make_mock_db(calls)
        cols = [ColumnInfo(name="x", type="VARCHAR")]
        model = ModelInfo(name="t", database="db", schema="main", columns=cols)
        project = ProjectInfo(project_name="p", adapter_type="postgres", models=[model])
        d = SchemaDiscovery(project, db=db)
        asyncio.run(d.describe_table("t"))
        calls.clear()
        asyncio.run(d.describe_table("t"))
        assert calls == [], "cached result must not re-issue DB queries"


class TestDistinctValuesKeyMismatch:
    """_get_distinct_values must not rely on the result dict key matching the IR column name.

    Some adapters normalise identifier case differently from what dbt stores in
    catalog.json; using the first value in the result row is always correct.
    """

    def _make_mismatched_key_db(self):
        class _MockDb:
            dialect_name = "postgresql"

            async def execute_text(self, sql: str) -> list[dict]:
                if "COUNT(*)" in sql:
                    return [{"cnt": 0}]
                if "DISTINCT" in sql:
                    # Simulate DB returning lowercase key; IR name is mixed case.
                    return [{"customerid": "Alice"}, {"customerid": "Bob"}]
                return []

        return _MockDb()

    def test_returns_values_when_result_key_differs_from_column_name(self):
        db = self._make_mismatched_key_db()
        col = ColumnInfo(name="customerID", type="VARCHAR")
        model = ModelInfo(name="t", database="db", schema="main", columns=[col])
        project = ProjectInfo(
            project_name="p", adapter_type="postgresql", models=[model]
        )
        d = SchemaDiscovery(project, db=db)
        detail = asyncio.run(d.describe_table("t"))
        assert detail is not None
        vs = detail.columns[0].value_summary
        assert vs is not None
        assert vs["kind"] == "distinct"
        assert vs["values"] == ["Alice", "Bob"]
