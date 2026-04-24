"""Tests for the shared sqlglot plumbing in compiled_sql.py."""

from pathlib import Path

import pytest

from dbt_graphql.dbt.artifacts import load_catalog, load_manifest
from dbt_graphql.dbt.processors.compiled_sql import (
    build_schema_for_model,
    build_table_lookup,
    detect_dialect,
    qualify_model_sql,
)

FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


class TestBuildTableLookup:
    def test_maps_relation_names_to_model_names(self):
        manifest = load_manifest(MANIFEST)
        lookup = build_table_lookup(manifest)
        # relation_name is '"jaffle_shop"."main"."customers"' → normalized lower
        assert lookup["jaffle_shop.main.customers"] == "customers"
        assert lookup["jaffle_shop.main.orders"] == "orders"

    def test_schema_alias_resolves_to_model_name(self):
        manifest = load_manifest(MANIFEST)
        lookup = build_table_lookup(manifest)
        # schema.alias form: "main.customers"
        assert lookup["main.customers"] == "customers"
        assert lookup["main.stg_customers"] == "stg_customers"

    def test_alias_only_not_present(self):
        # Alias-only key intentionally excluded to prevent cross-package collisions
        manifest = load_manifest(MANIFEST)
        lookup = build_table_lookup(manifest)
        assert "customers" not in lookup
        assert "stg_customers" not in lookup

    def test_includes_seeds(self):
        manifest = load_manifest(MANIFEST)
        lookup = build_table_lookup(manifest)
        # Seeds are keyed by schema.alias (e.g. "main.raw_customers")
        assert lookup["main.raw_customers"] == "raw_customers"
        assert lookup["main.raw_orders"] == "raw_orders"


class TestBuildSchemaForModel:
    def test_contains_only_parent_models(self):
        manifest = load_manifest(MANIFEST)
        catalog = load_catalog(CATALOG)
        customers = manifest.nodes["model.jaffle_shop.customers"]
        schema = build_schema_for_model(customers, manifest, catalog)
        # depends_on: stg_customers, stg_orders, stg_payments
        db = "jaffle_shop"
        sch = "main"
        tables = schema[db][sch]
        assert set(tables.keys()) == {"stg_customers", "stg_orders", "stg_payments"}

    def test_column_types_present(self):
        manifest = load_manifest(MANIFEST)
        catalog = load_catalog(CATALOG)
        customers = manifest.nodes["model.jaffle_shop.customers"]
        schema = build_schema_for_model(customers, manifest, catalog)
        cols = schema["jaffle_shop"]["main"]["stg_customers"]
        assert "customer_id" in cols
        assert cols["customer_id"]  # non-empty type string


class TestDetectDialect:
    def test_detect_dialect_from_manifest(self):
        manifest = load_manifest(MANIFEST)
        assert detect_dialect(manifest) == manifest.metadata.adapter_type

    def test_sqlserver_maps_to_tsql(self):
        class _Meta:
            adapter_type = "sqlserver"

        class _Man:
            metadata = _Meta()

        assert detect_dialect(_Man()) == "tsql"

    def test_missing_adapter_raises(self):
        class _Meta:
            adapter_type = None

        class _Man:
            metadata = _Meta()

        with pytest.raises(ValueError, match="adapter_type"):
            detect_dialect(_Man())


class TestQualifyModelSql:
    def test_returns_scope_for_valid_sql(self):
        sql = 'SELECT a FROM "db"."sch"."t"'
        schema = {"db": {"sch": {"t": {"a": "INTEGER"}}}}
        scope = qualify_model_sql(sql, "postgres", schema)
        assert scope is not None
        assert hasattr(scope, "sources")

    def test_returns_none_for_empty_sql(self):
        assert qualify_model_sql("", "postgres", {}) is None

    def test_returns_none_for_unparseable_sql(self):
        assert qualify_model_sql("NOT VALID SQL !!!", "postgres", {}) is None
