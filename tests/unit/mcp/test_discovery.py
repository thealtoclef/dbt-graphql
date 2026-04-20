"""Tests for SchemaDiscovery (no live DB required)."""

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
        detail = d.describe_table("customers")
        assert detail is not None
        col_names = {c.name for c in detail.columns}
        assert "customer_id" in col_names

    def test_missing_table_returns_none(self):
        d = _make_discovery()
        assert d.describe_table("nonexistent") is None


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
        assert customers_rel.direction in ("outgoing", "incoming")


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
        from_column=from_col,
        to_model=to_model,
        to_column=to_col,
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
            adapter_type="duckdb",
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
            adapter_type="duckdb",
            models=models,
            relationships=rels,
        )
        d = SchemaDiscovery(project)
        assert d.find_path("A", "D") == []
