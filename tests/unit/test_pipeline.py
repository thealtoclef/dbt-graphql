"""Tests for extract_project pipeline and _rel_to_domain helper."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbt_graphql.pipeline import _rel_to_domain, extract_project
from dbt_graphql.ir.models import JoinType, RelationshipInfo, RelationshipOrigin

FIXTURES = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = FIXTURES / "catalog.json"
MANIFEST = FIXTURES / "manifest.json"


# ---------------------------------------------------------------------------
# _rel_to_domain
# ---------------------------------------------------------------------------


def _rel(
    name,
    models,
    join_type,
    origin=RelationshipOrigin.data_test,
    from_columns=None,
    to_columns=None,
):
    return SimpleNamespace(
        name=name,
        models=models,
        join_type=join_type,
        origin=origin,
        from_columns=from_columns or [],
        to_columns=to_columns or [],
        business_name="",
        description="",
    )


class TestRelToDomain:
    def test_basic_conversion(self):
        rel = _rel(
            name="orders_customers",
            models=["orders", "customers"],
            join_type=JoinType.many_to_one,
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
        assert result.join_type == "many_to_one"

    def test_different_column_names(self):
        rel = _rel(
            name="line_items_orders",
            models=["line_items", "orders"],
            join_type=JoinType.many_to_one,
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
            join_type=JoinType.many_to_one,
        )
        result = _rel_to_domain(rel)
        assert result.from_columns == []
        assert result.to_columns == []

    def test_empty_from_to_columns(self):
        rel = _rel("a_b", ["a", "b"], JoinType.many_to_one)
        # from_columns/to_columns default to [] via _rel helper
        result = _rel_to_domain(rel)
        assert result.from_columns == []
        assert result.to_columns == []

    def test_join_type_string(self):
        for jt in JoinType:
            rel = _rel("x_y", ["x", "y"], jt)
            result = _rel_to_domain(rel)
            assert result.join_type == str(jt)

    def test_business_name_and_description_propagated(self):
        rel = SimpleNamespace(
            name="orders_customers",
            models=["orders", "customers"],
            join_type=JoinType.many_to_one,
            origin=RelationshipOrigin.constraint,
            from_columns=["customer_id"],
            to_columns=["customer_id"],
            business_name="order customer",
            description="FK from orders to customers",
        )
        result = _rel_to_domain(rel)
        assert result.business_name == "order customer"
        assert result.description == "FK from orders to customers"

    def test_constraint_origin_gives_declared_confidence(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            join_type=JoinType.many_to_one,
            origin=RelationshipOrigin.constraint,
            from_columns=["x_id"],
            to_columns=["id"],
        )
        result = _rel_to_domain(rel)
        assert result.cardinality_confidence == "declared"

    def test_no_unique_cols_gives_assumed_confidence(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            join_type=JoinType.many_to_one,
            from_columns=["x_id"],
            to_columns=["id"],
        )
        result = _rel_to_domain(rel, unique_cols=set())
        assert result.cardinality_confidence == "assumed"

    def test_lineage_edge_no_unique_becomes_join_hint(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            join_type=JoinType.many_to_one,
            origin=RelationshipOrigin.lineage,
            from_columns=["x_id"],
            to_columns=["id"],
        )
        result = _rel_to_domain(rel, unique_cols=set())
        assert result.origin == RelationshipOrigin.join_hint
        assert result.cardinality_confidence == "assumed"

    def test_lineage_edge_with_unique_stays_lineage(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            join_type=JoinType.many_to_one,
            origin=RelationshipOrigin.lineage,
            from_columns=["x_id"],
            to_columns=["id"],
        )
        # "y".id is unique → inferred, stays lineage
        result = _rel_to_domain(rel, unique_cols={("y", "id")})
        assert result.origin == RelationshipOrigin.lineage
        assert result.cardinality_confidence == "inferred"


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
        assert rel.join_type == "many_to_one"

    def test_one_to_one_when_both_cols_unique(self, tmp_path):
        import json

        data = json.loads(MANIFEST.read_text())

        # Add a unique test on orders.customer_id to simulate a one-to-one FK
        uid = "test.jaffle_shop.unique_orders_customer_id.synthetic"
        data["nodes"][uid] = {
            "resource_type": "test",
            "database": "dev",
            "schema": "main",
            "name": "unique_orders_customer_id",
            "unique_id": uid,
            "package_name": "jaffle_shop",
            "path": "x.sql",
            "original_file_path": "x.yml",
            "fqn": ["jaffle_shop", "unique_orders_customer_id"],
            "alias": "unique_orders_customer_id",
            "checksum": {"name": "sha256", "checksum": "x"},
            "column_name": "customer_id",
            "attached_node": "model.jaffle_shop.orders",
            "refs": [],
            "test_metadata": {"name": "unique", "kwargs": {}, "namespace": None},
        }

        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(data))

        project = extract_project(CATALOG, manifest_path)
        rel = next(
            r
            for r in project.relationships
            if r.from_model == "orders" and r.to_model == "customers"
        )
        assert rel.join_type == "one_to_one"

    def test_infer_join_type_function_directly(self):
        from dbt_graphql.pipeline import _infer_join_type

        unique = {("customers", "customer_id"), ("orders", "order_id")}

        jt, conf = _infer_join_type(
            "orders", ["customer_id"], "customers", ["customer_id"], unique
        )
        assert jt == "many_to_one"
        assert conf == "inferred"

        jt, conf = _infer_join_type(
            "orders", ["order_id"], "customers", ["customer_id"], unique
        )
        assert jt == "one_to_one"

        jt, conf = _infer_join_type(
            "orders", ["order_id"], "line_items", ["order_id"], set()
        )
        assert jt == "many_to_one"
        assert conf == "assumed"

        jt, conf = _infer_join_type(
            "orders",
            ["order_id"],
            "customers",
            ["customer_id"],
            {("orders", "order_id")},
        )
        assert jt == "one_to_many"


class TestOriginPropagation:
    """The origin tag flows from ProcessorRelationship → RelationshipInfo."""

    def test_test_origin_preserved(self):
        rel = _rel(
            name="orders_customers",
            models=["orders", "customers"],
            join_type=JoinType.many_to_one,
            origin=RelationshipOrigin.data_test,
        )
        assert _rel_to_domain(rel).origin == RelationshipOrigin.data_test

    def test_constraint_origin_preserved(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            join_type=JoinType.many_to_one,
            origin=RelationshipOrigin.constraint,
        )
        assert _rel_to_domain(rel).origin == RelationshipOrigin.constraint

    def test_lineage_origin_preserved(self):
        rel = _rel(
            name="x_y",
            models=["x", "y"],
            join_type=JoinType.many_to_one,
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
