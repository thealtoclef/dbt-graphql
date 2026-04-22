from pathlib import Path

from dbt_graphql.dbt.artifacts import load_manifest
from dbt_graphql.dbt.processors.compiled_sql import extract_table_lineage
from dbt_graphql.ir.models import TableLineageItem


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


class TestTableLineage:
    def test_customers_depends_on_three_stg_models(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        customers_sources = {e.source for e in result if e.target == "customers"}
        assert customers_sources == {"stg_customers", "stg_orders", "stg_payments"}

    def test_orders_depends_on_two_stg_models(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        orders_sources = {e.source for e in result if e.target == "orders"}
        assert orders_sources == {"stg_orders", "stg_payments"}

    def test_stg_models_depend_on_seeds(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        assert {e.source for e in result if e.target == "stg_customers"} == {
            "raw_customers"
        }
        assert {e.source for e in result if e.target == "stg_orders"} == {"raw_orders"}
        assert {e.source for e in result if e.target == "stg_payments"} == {
            "raw_payments"
        }

    def test_only_model_nodes_as_targets(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        known_models = {
            "customers",
            "orders",
            "stg_customers",
            "stg_orders",
            "stg_payments",
        }
        assert {e.target for e in result} <= known_models

    def test_returns_list_of_table_lineage_items(self):
        manifest = load_manifest(MANIFEST)
        result = extract_table_lineage(manifest)
        assert isinstance(result, list)
        assert all(isinstance(e, TableLineageItem) for e in result)


class TestConvertResultLineage:
    def test_convert_result_has_lineage(self):
        from dbt_graphql import extract_project

        project = extract_project(CATALOG, MANIFEST)
        lineage = project.build_lineage_schema()
        assert lineage is not None

    def test_lineage_schema_serialization(self):
        from dbt_graphql import extract_project

        project = extract_project(CATALOG, MANIFEST)
        lineage = project.build_lineage_schema()
        json_str = lineage.model_dump_json(by_alias=True, indent=2)
        assert "tableLineage" in json_str
        assert "columnLineage" in json_str
