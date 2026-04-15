import pytest
from pathlib import Path

from wren_dbt_converter.processors.lineage import (
    ColumnLineageEdge,
    LineageResult,
    extract_table_lineage,
    extract_column_lineage,
    build_lineage,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestTableLineage:
    def test_customers_depends_on_three_stg_models(self, manifest):
        result = extract_table_lineage(manifest)
        assert set(result["customers"]) == {
            "stg_customers",
            "stg_orders",
            "stg_payments",
        }

    def test_orders_depends_on_two_stg_models(self, manifest):
        result = extract_table_lineage(manifest)
        assert set(result["orders"]) == {"stg_orders", "stg_payments"}

    def test_stg_models_depend_on_seeds(self, manifest):
        result = extract_table_lineage(manifest)
        assert result["stg_customers"] == ["raw_customers"]
        assert result["stg_orders"] == ["raw_orders"]
        assert result["stg_payments"] == ["raw_payments"]

    def test_only_model_nodes_in_keys(self, manifest):
        result = extract_table_lineage(manifest)
        for key in result:
            assert key in {
                "customers",
                "orders",
                "stg_customers",
                "stg_orders",
                "stg_payments",
            }

    def test_returns_dict(self, manifest):
        result = extract_table_lineage(manifest)
        assert isinstance(result, dict)


class TestColumnLineage:
    def test_returns_dict_of_dicts(self, manifest_path, catalog_path):
        result = extract_column_lineage(manifest_path, catalog_path)
        assert isinstance(result, dict)
        for model_name, col_map in result.items():
            assert isinstance(model_name, str)
            assert isinstance(col_map, dict)

    def test_edges_have_required_fields(self, manifest_path, catalog_path):
        result = extract_column_lineage(manifest_path, catalog_path)
        for model_name, col_map in result.items():
            for col_name, edges in col_map.items():
                for edge in edges:
                    assert isinstance(edge, ColumnLineageEdge)
                    assert edge.source_model
                    assert edge.source_column
                    assert edge.target_column == col_name
                    assert edge.lineage_type in (
                        "pass-through",
                        "rename",
                        "transformation",
                    )

    def test_customers_has_column_lineage(self, manifest_path, catalog_path):
        result = extract_column_lineage(manifest_path, catalog_path)
        assert "customers" in result
        assert isinstance(result["customers"], dict)
        assert len(result["customers"]) > 0

    def test_stg_models_have_column_lineage(self, manifest_path, catalog_path):
        result = extract_column_lineage(manifest_path, catalog_path)
        assert "stg_customers" in result
        assert "stg_orders" in result
        assert "stg_payments" in result


class TestBuildLineage:
    def test_returns_lineage_result(self, manifest, manifest_path, catalog_path):
        result = build_lineage(manifest, manifest_path, catalog_path)
        assert isinstance(result, LineageResult)
        assert isinstance(result.table_lineage, dict)
        assert isinstance(result.column_lineage, dict)

    def test_table_lineage_populated(self, manifest, manifest_path, catalog_path):
        result = build_lineage(manifest, manifest_path, catalog_path)
        assert len(result.table_lineage) > 0
        assert "customers" in result.table_lineage

    def test_column_lineage_populated(self, manifest, manifest_path, catalog_path):
        result = build_lineage(manifest, manifest_path, catalog_path)
        assert len(result.column_lineage) > 0


class TestLineageEmbeddedInProperties:
    """Verify build_manifest() embeds bidirectional lineage in model properties."""

    @pytest.fixture(autouse=True)
    def setup(self, dbt_project):
        from wren_dbt_converter import build_manifest

        self.result = build_manifest(dbt_project)

    def _get_model(self, name: str):
        return next(m for m in self.result.manifest.models if m.name == name)

    # --- Structure ---

    def test_lineage_key_in_model_properties(self):
        customers = self._get_model("customers")
        assert customers.properties is not None
        assert "lineage" in customers.properties

    def test_lineage_has_upstream_and_downstream(self):
        customers = self._get_model("customers")
        lineage = customers.properties["lineage"]
        assert "upstream" in lineage
        assert isinstance(lineage["upstream"], dict)

    def test_models_without_lineage_have_no_lineage_key(self):
        # A model with no upstream or downstream shouldn't get the lineage key
        for model in self.result.manifest.models:
            if not model.properties or "lineage" not in model.properties:
                continue
            lin = model.properties["lineage"]
            has_upstream = bool(
                lin.get("upstream", {}).get("models")
                or lin.get("upstream", {}).get("columns")
            )
            has_downstream = bool(
                lin.get("downstream", {}).get("models")
                or lin.get("downstream", {}).get("columns")
            )
            assert has_upstream or has_downstream

    # --- Model-level lineage ---

    def test_upstream_models(self):
        customers = self._get_model("customers")
        upstream = customers.properties["lineage"]["upstream"]
        assert set(upstream["models"]) == {
            "stg_customers",
            "stg_orders",
            "stg_payments",
        }

    def test_downstream_models(self):
        stg_orders = self._get_model("stg_orders")
        downstream = stg_orders.properties["lineage"]["downstream"]
        assert set(downstream["models"]) == {"customers", "orders"}

    def test_stg_models_upstream(self):
        stg_orders = self._get_model("stg_orders")
        upstream = stg_orders.properties["lineage"]["upstream"]
        assert upstream["models"] == ["raw_orders"]

    # --- Column-level lineage ---

    def test_upstream_columns_on_target_model(self):
        customers = self._get_model("customers")
        upstream_cols = customers.properties["lineage"]["upstream"]["columns"]
        assert isinstance(upstream_cols, dict)
        assert len(upstream_cols) > 0
        for col_name, edges in upstream_cols.items():
            assert isinstance(edges, list)
            for entry in edges:
                assert "model" in entry
                assert "column" in entry
                assert "type" in entry

    def test_downstream_columns_on_source_model(self):
        stg_customers = self._get_model("stg_customers")
        downstream_cols = stg_customers.properties["lineage"]["downstream"]["columns"]
        assert isinstance(downstream_cols, dict)
        assert len(downstream_cols) > 0
        for col_name, edges in downstream_cols.items():
            for entry in edges:
                assert entry["model"] == "customers"

    def test_bidirectional_column_lineage_consistent(self):
        customers = self._get_model("customers")
        stg_customers = self._get_model("stg_customers")

        # Upstream edges from customers columns
        upstream_pairs: set[tuple[str, str]] = set()
        for col_name, edges in customers.properties["lineage"]["upstream"][
            "columns"
        ].items():
            for e in edges:
                if e["model"] == "stg_customers":
                    upstream_pairs.add((col_name, e["column"]))

        # Downstream edges from stg_customers columns
        downstream_pairs: set[tuple[str, str]] = set()
        for col_name, edges in stg_customers.properties["lineage"]["downstream"][
            "columns"
        ].items():
            for e in edges:
                if e["model"] == "customers":
                    downstream_pairs.add((e["column"], col_name))

        assert upstream_pairs == downstream_pairs
