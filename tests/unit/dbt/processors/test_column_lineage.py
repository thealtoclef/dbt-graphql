"""Tests for sqlglot-based column lineage extraction."""

import json
from pathlib import Path

from dbt_artifacts_parser.parser import parse_manifest

from dbt_graphql.dbt.artifacts import load_catalog, load_manifest
from dbt_graphql.dbt.processors.compiled_sql import (
    _edges_for_model,
    extract_column_lineage,
    qualify_model_sql,
)
from dbt_graphql.ir.models import Column, ColumnLineageItem, LineageType

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


def _edges(sql: str) -> list[ColumnLineageItem]:
    scope = qualify_model_sql(sql, "duckdb", _SCHEMA)
    assert scope is not None, f"qualify failed for SQL: {sql}"
    return _edges_for_model("t", scope, _TABLE_LOOKUP, "duckdb")


def _cols(items: list[ColumnLineageItem], target_col: str) -> list[tuple[str, Column]]:
    """Return (source_model, Column) for every column mapping targeting target_col."""
    return [
        (item.source, col)
        for item in items
        for col in item.columns
        if col.target_column == target_col
    ]


class TestClassification:
    def test_pass_through(self):
        items = _edges('SELECT a FROM "db"."sch"."t"')
        cols = _cols(items, "a")
        assert len(cols) == 1
        source, col = cols[0]
        assert source == "t"
        assert col.source_column == "a"
        assert col.lineage_type == LineageType.pass_through

    def test_rename(self):
        items = _edges('SELECT a AS b FROM "db"."sch"."t"')
        cols = _cols(items, "b")
        assert len(cols) == 1
        _, col = cols[0]
        assert col.source_column == "a"
        assert col.lineage_type == LineageType.rename

    def test_pass_through_alias_same_name(self):
        items = _edges('SELECT a AS a FROM "db"."sch"."t"')
        cols = _cols(items, "a")
        assert len(cols) == 1
        _, col = cols[0]
        assert col.lineage_type == LineageType.pass_through

    def test_transformation_function(self):
        items = _edges('SELECT UPPER(b) AS upper_b FROM "db"."sch"."t"')
        cols = _cols(items, "upper_b")
        assert len(cols) == 1
        _, col = cols[0]
        assert col.source_column == "b"
        assert col.lineage_type == LineageType.transformation

    def test_multi_source(self):
        sql = (
            "SELECT COALESCE(a.x, b.x) AS x_coalesced "
            'FROM "db"."sch"."src_a" a '
            'JOIN "db"."sch"."src_b" b ON a.x = b.x'
        )
        items = _edges(sql)
        cols = _cols(items, "x_coalesced")
        sources = {(src, col.source_column) for src, col in cols}
        assert sources == {("src_a", "x"), ("src_b", "x")}
        for _, col in cols:
            assert col.lineage_type == LineageType.transformation


class TestCteResolution:
    def test_column_traced_through_cte(self):
        sql = 'WITH wrapped AS (SELECT a FROM "db"."sch"."t") SELECT a FROM wrapped'
        items = _edges(sql)
        cols = _cols(items, "a")
        assert len(cols) == 1
        source, col = cols[0]
        assert source == "t"
        assert col.source_column == "a"
        assert col.lineage_type == LineageType.pass_through

    def test_transformation_inside_cte_bubbles_up(self):
        sql = (
            'WITH wrapped AS (SELECT UPPER(b) AS upper_b FROM "db"."sch"."t") '
            "SELECT upper_b FROM wrapped"
        )
        items = _edges(sql)
        cols = _cols(items, "upper_b")
        assert len(cols) == 1
        _, col = cols[0]
        assert col.source_column == "b"
        assert col.lineage_type == LineageType.transformation


class TestExtractColumnLineage:
    def test_skips_models_without_compiled_code(self, tmp_path):
        data = json.loads(MANIFEST.read_text())
        for uid, node in data["nodes"].items():
            if uid == "model.jaffle_shop.customers":
                node["compiled_code"] = ""

        man = parse_manifest(data)
        cat = load_catalog(CATALOG)
        result = extract_column_lineage(man, cat)
        assert not any(item.target == "customers" for item in result)
        assert any(item.target == "stg_customers" for item in result)

    def test_jaffle_shop_integration(self):
        man = load_manifest(MANIFEST)
        cat = load_catalog(CATALOG)
        result = extract_column_lineage(man, cat)

        # customers.customer_id should trace to stg_customers.customer_id
        cid_sources = {
            (item.source, col.source_column)
            for item in result
            if item.target == "customers"
            for col in item.columns
            if col.target_column == "customer_id"
        }
        assert ("stg_customers", "customer_id") in cid_sources

        # stg_customers.customer_id is a rename of raw_customers.id
        stg_item = next(
            (
                item
                for item in result
                if item.target == "stg_customers" and item.source == "raw_customers"
            ),
            None,
        )
        assert stg_item is not None
        cid_col = next(
            (col for col in stg_item.columns if col.target_column == "customer_id"),
            None,
        )
        assert cid_col is not None
        assert cid_col.source_column == "id"
        assert cid_col.lineage_type == LineageType.rename

    def test_returns_list_of_items(self):
        man = load_manifest(MANIFEST)
        cat = load_catalog(CATALOG)
        result = extract_column_lineage(man, cat)
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, ColumnLineageItem)
            assert item.source
            assert item.target
            for col in item.columns:
                assert isinstance(col, Column)
                assert col.target_column
                assert col.lineage_type in {
                    LineageType.pass_through,
                    LineageType.rename,
                    LineageType.transformation,
                }
