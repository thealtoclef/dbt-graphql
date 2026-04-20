"""Tests for sqlglot-based column lineage extraction."""

import json
from pathlib import Path

from dbt_artifacts_parser.parser import parse_manifest

from dbt_graphql.dbt.artifacts import load_catalog, load_manifest
from dbt_graphql.dbt.processors.compiled_sql import (
    ColumnLineageEdge,
    _edges_for_model,
    extract_column_lineage,
    qualify_model_sql,
)

FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


_TABLE_LOOKUP = {
    "db.sch.t": "t",
    "db.sch.src_a": "src_a",
    "db.sch.src_b": "src_b",
}

_SCHEMA = {
    "db": {
        "sch": {
            "t": {"a": "INTEGER", "b": "VARCHAR"},
            "src_a": {"x": "INTEGER", "y": "VARCHAR"},
            "src_b": {"x": "INTEGER", "z": "VARCHAR"},
        }
    }
}


def _edges(sql: str) -> dict[str, list[ColumnLineageEdge]]:
    scope = qualify_model_sql(sql, "duckdb", _SCHEMA)
    assert scope is not None, f"qualify failed for SQL: {sql}"
    return _edges_for_model(scope, _TABLE_LOOKUP, "duckdb")


class TestClassification:
    def test_pass_through(self):
        edges = _edges('SELECT a FROM "db"."sch"."t"')
        e = edges["a"][0]
        assert e.source_model == "t"
        assert e.source_column == "a"
        assert e.lineage_type == "pass_through"

    def test_rename(self):
        edges = _edges('SELECT a AS b FROM "db"."sch"."t"')
        e = edges["b"][0]
        assert e.source_column == "a"
        assert e.lineage_type == "rename"

    def test_pass_through_alias_same_name(self):
        edges = _edges('SELECT a AS a FROM "db"."sch"."t"')
        e = edges["a"][0]
        assert e.lineage_type == "pass_through"

    def test_transformation_function(self):
        edges = _edges('SELECT UPPER(b) AS upper_b FROM "db"."sch"."t"')
        e = edges["upper_b"][0]
        assert e.source_column == "b"
        assert e.lineage_type == "transformation"

    def test_multi_source(self):
        sql = (
            "SELECT COALESCE(a.x, b.x) AS x_coalesced "
            'FROM "db"."sch"."src_a" a '
            'JOIN "db"."sch"."src_b" b ON a.x = b.x'
        )
        edges = _edges(sql)
        sources = {(e.source_model, e.source_column) for e in edges["x_coalesced"]}
        assert sources == {("src_a", "x"), ("src_b", "x")}
        for e in edges["x_coalesced"]:
            assert e.lineage_type == "transformation"


class TestCteResolution:
    def test_column_traced_through_cte(self):
        sql = 'WITH wrapped AS (SELECT a FROM "db"."sch"."t") SELECT a FROM wrapped'
        edges = _edges(sql)
        e = edges["a"][0]
        assert e.source_model == "t"
        assert e.source_column == "a"
        assert e.lineage_type == "pass_through"

    def test_transformation_inside_cte_bubbles_up(self):
        sql = (
            'WITH wrapped AS (SELECT UPPER(b) AS upper_b FROM "db"."sch"."t") '
            "SELECT upper_b FROM wrapped"
        )
        edges = _edges(sql)
        e = edges["upper_b"][0]
        assert e.source_column == "b"
        assert e.lineage_type == "transformation"


class TestExtractColumnLineage:
    def test_skips_models_without_compiled_code(self, tmp_path):
        data = json.loads(MANIFEST.read_text())
        # Drop compiled_code from customers
        for uid, node in data["nodes"].items():
            if uid == "model.jaffle_shop.customers":
                node["compiled_code"] = ""

        man = parse_manifest(data)
        cat = load_catalog(CATALOG)
        result = extract_column_lineage(man, cat)
        assert "customers" not in result
        # Other models still present
        assert "stg_customers" in result

    def test_jaffle_shop_integration(self):
        man = load_manifest(MANIFEST)
        cat = load_catalog(CATALOG)
        result = extract_column_lineage(man, cat)

        # customers.customer_id should trace to stg_customers.customer_id
        customers_cols = result["customers"]
        cid_sources = {
            (e.source_model, e.source_column) for e in customers_cols["customer_id"]
        }
        assert ("stg_customers", "customer_id") in cid_sources

        # stg_customers.customer_id is a rename of raw_customers.id
        stg = result["stg_customers"]
        cid_edge = next(e for e in stg["customer_id"])
        assert cid_edge.source_model == "raw_customers"
        assert cid_edge.source_column == "id"
        assert cid_edge.lineage_type == "rename"

    def test_returns_dict_of_lists_of_edges(self):
        man = load_manifest(MANIFEST)
        cat = load_catalog(CATALOG)
        result = extract_column_lineage(man, cat)
        for model, col_map in result.items():
            assert isinstance(col_map, dict)
            for col, edges in col_map.items():
                assert isinstance(edges, list)
                for e in edges:
                    assert isinstance(e, ColumnLineageEdge)
                    assert e.target_column == col
                    assert e.lineage_type in {
                        "pass_through",
                        "rename",
                        "transformation",
                    }
