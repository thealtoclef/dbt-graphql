"""Tests for SchemaDiscovery (manifest-only)."""

from pathlib import Path

from dbt_graphql.formatter.graphql import build_registry
from dbt_graphql.pipeline import extract_project
from dbt_graphql.mcp.discovery import SchemaDiscovery
from dbt_graphql.ir.models import (
    Cardinality,
    ColumnInfo,
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
    return SchemaDiscovery(build_registry(project), project=project)


def _discovery_from(project: ProjectInfo, **kwargs) -> SchemaDiscovery:
    return SchemaDiscovery(build_registry(project), project=project, **kwargs)


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

    def test_enum_values_carried_from_manifest(self):
        col = ColumnInfo(
            name="status", type="VARCHAR", enum_values=["placed", "shipped"]
        )
        model = ModelInfo(name="orders", database="db", schema="main", columns=[col])
        project = ProjectInfo(
            project_name="test", adapter_type="postgres", models=[model]
        )
        d = _discovery_from(project)
        detail = d.describe_table("orders")
        assert detail is not None
        status_col = next(c for c in detail.columns if c.name == "status")
        assert status_col.enum_values == ["placed", "shipped"]


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


def _make_model(name: str, *fk_cols: str) -> ModelInfo:
    """Build a model with an ``id`` column plus optional FK columns.

    Hand-rolled relationships need an actual column on the source model
    or ``build_registry`` won't pick them up — the registry only carries
    relations attached to a real column.
    """
    cols = [_make_col("id"), *(_make_col(c) for c in fk_cols)]
    return ModelInfo(
        name=name,
        database="db",
        schema="main",
        columns=cols,
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
        cardinality=Cardinality.many_to_one,
        origin=RelationshipOrigin.data_test,
    )


class TestFindPathDiamond:
    """Verify that BFS returns all shortest paths, not just the first found.

    Diamond topology: A→B→D and A→C→D (both length 2 through distinct intermediates).
    """

    def _diamond_discovery(self) -> SchemaDiscovery:
        models = [
            _make_model("A", "b_id", "c_id"),
            _make_model("B", "d_id"),
            _make_model("C", "d_id"),
            _make_model("D"),
        ]
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
        return _discovery_from(project)

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
        models = [
            _make_model("A", "b_id"),
            _make_model("B"),
            _make_model("C", "d_id"),
            _make_model("D"),
        ]
        rels = [_make_rel("A", "b_id", "B", "id"), _make_rel("C", "d_id", "D", "id")]
        project = ProjectInfo(
            project_name="test",
            adapter_type="postgres",
            models=models,
            relationships=rels,
        )
        d = _discovery_from(project)
        assert d.find_path("A", "D") == []
