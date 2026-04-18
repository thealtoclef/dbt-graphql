import pytest
from pathlib import Path

from dbt_mdl.dbt.artifacts import load_manifest
from dbt_mdl.dbt.processors.lineage import (
    ColumnLineageEdge,
    extract_table_lineage,
    extract_column_lineage,
)


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


class TestTableLineage:
    def test_customers_depends_on_three_stg_models(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        assert set(result["customers"]) == {
            "stg_customers",
            "stg_orders",
            "stg_payments",
        }

    def test_orders_depends_on_two_stg_models(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        assert set(result["orders"]) == {"stg_orders", "stg_payments"}

    def test_stg_models_depend_on_seeds(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        assert result["stg_customers"] == ["raw_customers"]
        assert result["stg_orders"] == ["raw_orders"]
        assert result["stg_payments"] == ["raw_payments"]

    def test_only_model_nodes_in_keys(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        for key in result:
            assert key in {
                "customers",
                "orders",
                "stg_customers",
                "stg_orders",
                "stg_payments",
            }

    def test_returns_dict(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        assert isinstance(result, dict)


class TestColumnLineage:
    def test_returns_dict_of_dicts(self):
        result = extract_column_lineage(MANIFEST, CATALOG)
        assert isinstance(result, dict)
        for model_name, col_map in result.items():
            assert isinstance(model_name, str)
            assert isinstance(col_map, dict)

    def test_edges_have_required_fields(self):
        result = extract_column_lineage(MANIFEST, CATALOG)
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

    def test_customers_has_column_lineage(self):
        result = extract_column_lineage(MANIFEST, CATALOG)
        assert "customers" in result
        assert isinstance(result["customers"], dict)
        assert len(result["customers"]) > 0

    def test_stg_models_have_column_lineage(self):
        result = extract_column_lineage(MANIFEST, CATALOG)
        assert "stg_customers" in result
        assert "stg_orders" in result
        assert "stg_payments" in result


class TestConvertResultLineage:
    def test_convert_result_has_lineage(self):
        from dbt_mdl import extract_project

        project = extract_project(CATALOG, MANIFEST)
        lineage = project.build_lineage_schema()
        assert lineage is not None

    def test_lineage_schema_serialization(self):
        from dbt_mdl import extract_project

        project = extract_project(CATALOG, MANIFEST)
        lineage = project.build_lineage_schema()
        json_str = lineage.model_dump_json(by_alias=True, indent=2)
        assert "tableLineage" in json_str
        assert "columnLineage" in json_str
