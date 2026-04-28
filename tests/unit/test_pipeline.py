"""Tests for extract_project pipeline and _rel_to_domain helper."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbt_graphql.pipeline import _rel_to_domain, extract_project
from dbt_graphql.ir.models import Cardinality, RelationshipInfo, RelationshipOrigin

FIXTURES = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = FIXTURES / "catalog.json"
MANIFEST = FIXTURES / "manifest.json"


# ---------------------------------------------------------------------------
# _rel_to_domain
# ---------------------------------------------------------------------------


def _rel(
    name,
    models,
    cardinality,
    origin=RelationshipOrigin.data_test,
    from_columns=None,
    to_columns=None,
):
    return SimpleNamespace(
        name=name,
        models=models,
        cardinality=cardinality,
        origin=origin,
        from_columns=from_columns or [],
        to_columns=to_columns or [],
    )


class TestRelToDomain:
    def test_basic_conversion(self):
        rel = _rel(
            name="orders_customers",
            models=["orders", "customers"],
            cardinality=Cardinality.many_to_one,
            from_columns=["customer_id"],
            to_columns=["customer_id"],
        )
        result = _rel_to_domain(rel)
        assert isinstance(result, RelationshipInfo)
        assert result.name == "orders_customers"
        assert result.from_model == "orders"
        assert result.to_model == "customers"
        assert result.from_columns == ["customer_id"]
        assert result.to_columns == ["customer_id"]
        assert result.cardinality == "many_to_one"

    def test_different_column_names(self):
        rel = _rel(
            name="line_items_orders",
            models=["line_items", "orders"],
            cardinality=Cardinality.many_to_one,
            from_columns=["order_ref"],
            to_columns=["order_id"],
        )
        result = _rel_to_domain(rel)
        assert result.from_columns == ["order_ref"]
        assert result.to_columns == ["order_id"]

    def test_empty_columns(self):
        rel = _rel(
            name="a_b",
            models=["a", "b"],
            cardinality=Cardinality.many_to_one,
        )
        result = _rel_to_domain(rel)
        assert result.from_columns == []
        assert result.to_columns == []

    def test_empty_from_to_columns(self):
        rel = _rel("a_b", ["a", "b"], Cardinality.many_to_one)
        # from_columns/to_columns default to [] via _rel helper
        result = _rel_to_domain(rel)
        assert result.from_columns == []
        assert result.to_columns == []

    def test_cardinality_string(self):
        for jt in Cardinality:
            rel = _rel("x_y", ["x", "y"], jt)
            result = _rel_to_domain(rel)
            assert result.cardinality == str(jt)

    def test_lineage_edge_with_unique_stays_lineage(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            cardinality=Cardinality.many_to_one,
            origin=RelationshipOrigin.lineage,
            from_columns=["x_id"],
            to_columns=["id"],
        )
        # "y".id is unique → inferred, stays lineage
        result = _rel_to_domain(rel, unique_cols={("y", "id")})
        assert result.origin == RelationshipOrigin.lineage


class TestExtractProjectErrors:
    def test_missing_catalog_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="catalog.json"):
            extract_project(
                catalog_path=tmp_path / "catalog.json",
                manifest_path=MANIFEST,
            )

    def test_missing_manifest_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            extract_project(
                catalog_path=CATALOG,
                manifest_path=tmp_path / "manifest.json",
            )


class TestExtractProjectOutput:
    def test_returns_project_info_with_models(self):
        project = extract_project(CATALOG, MANIFEST)
        assert len(project.models) > 0

    def test_known_models_present(self):
        project = extract_project(CATALOG, MANIFEST)
        names = {m.name for m in project.models}
        assert "customers" in names
        assert "orders" in names

    def test_columns_are_sorted_by_index(self):
        project = extract_project(CATALOG, MANIFEST)
        customers = next(m for m in project.models if m.name == "customers")
        assert customers.columns[0].name == "customer_id"

    def test_relationships_extracted(self):
        project = extract_project(CATALOG, MANIFEST)
        assert len(project.relationships) > 0

    def test_relationships_have_from_and_to(self):
        project = extract_project(CATALOG, MANIFEST)
        for rel in project.relationships:
            assert rel.from_model
            assert rel.to_model

    def test_exclude_pattern_removes_models(self):
        project = extract_project(CATALOG, MANIFEST, exclude_patterns=[r"^stg_"])
        names = {m.name for m in project.models}
        assert not any(n.startswith("stg_") for n in names)
        assert "customers" in names

    def test_multiple_exclude_patterns(self):
        project = extract_project(
            CATALOG, MANIFEST, exclude_patterns=[r"^stg_", r"^orders$"]
        )
        names = {m.name for m in project.models}
        assert "orders" not in names
        assert "customers" in names

    def test_relationships_attached_to_models(self):
        project = extract_project(CATALOG, MANIFEST)
        orders = next(m for m in project.models if m.name == "orders")
        assert len(orders.relationships) > 0

    def test_adapter_type_from_manifest(self):
        project = extract_project(CATALOG, MANIFEST)
        assert project.adapter_type != ""


class TestCardinalityInference:
    def test_many_to_one_when_to_col_is_unique(self):
        # customers.customer_id has a unique test in the fixture → to_unique=True
        # orders.customer_id has no unique test → from_unique=False
        # → many_to_one
        project = extract_project(CATALOG, MANIFEST)
        rel = next(
            r
            for r in project.relationships
            if r.from_model == "orders" and r.to_model == "customers"
        )
        assert rel.cardinality == "many_to_one"


class TestOriginPropagation:
    """The origin tag flows from ProcessorRelationship → RelationshipInfo."""

    def test_test_origin_preserved(self):
        rel = _rel(
            name="orders_customers",
            models=["orders", "customers"],
            cardinality=Cardinality.many_to_one,
            origin=RelationshipOrigin.data_test,
        )
        assert _rel_to_domain(rel).origin == RelationshipOrigin.data_test

    def test_constraint_origin_preserved(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            cardinality=Cardinality.many_to_one,
            origin=RelationshipOrigin.constraint,
        )
        assert _rel_to_domain(rel).origin == RelationshipOrigin.constraint

    def test_lineage_origin_preserved(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            cardinality=Cardinality.many_to_one,
            origin=RelationshipOrigin.lineage,
        )
        assert _rel_to_domain(rel).origin == RelationshipOrigin.lineage

    def test_jaffle_shop_test_rel_has_test_origin(self):
        project = extract_project(CATALOG, MANIFEST)
        rel = next(
            r
            for r in project.relationships
            if r.from_model == "orders" and r.to_model == "customers"
        )
        assert rel.origin == RelationshipOrigin.data_test


class TestConstraintVsTestPriority:
    """Constraint-defined FKs take priority over test-inferred relationships."""

    def test_constraint_fk_deduplicates_test_relationship(self, tmp_path):
        raw = json.loads(MANIFEST.read_text())

        # Inject a model-level FK constraint into the orders manifest node
        orders_key = next(
            k for k in raw["nodes"] if k.startswith("model.") and k.endswith(".orders")
        )
        raw["nodes"][orders_key].setdefault("constraints", []).append(
            {
                "type": "foreign_key",
                "columns": ["customer_id"],
                "expression": "customers(customer_id)",
            }
        )

        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(raw))

        project = extract_project(CATALOG, manifest_path)

        # The FK from the constraint + the test-inferred one with the same name
        # should be deduplicated — only one relationship for this pair.
        matching = [
            r
            for r in project.relationships
            if r.from_model == "orders" and r.to_model == "customers"
        ]
        # There should be exactly one (not two) orders→customers relationship.
        assert len(matching) >= 1
        # The constraint-derived one should be first (it's added first).
        constraint_rel = matching[0]
        assert constraint_rel.from_columns == ["customer_id"]
        assert constraint_rel.to_columns == ["customer_id"]
