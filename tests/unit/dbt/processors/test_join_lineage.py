"""Tests for sqlglot-based join-derived relationship extraction."""

import json
from pathlib import Path

from dbt_artifacts_parser.parser import parse_manifest

from dbt_graphql.dbt.artifacts import load_catalog, load_manifest
from dbt_graphql.dbt.processors.compiled_sql import (
    _relationships_for_model,
    extract_join_relationships,
    qualify_model_sql,
)
from dbt_graphql.ir.models import RelationshipOrigin

FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


_LOOKUP = {
    "db.sch.m": "m",
    "db.sch.a": "a",
    "db.sch.b": "b",
    "db.sch.c": "c",
}

_SCHEMA = {
    "db": {
        "sch": {
            "m": {"id": "INTEGER", "a_id": "INTEGER"},
            "a": {"id": "INTEGER", "name": "VARCHAR"},
            "b": {"id": "INTEGER", "a_id": "INTEGER"},
            "c": {"id": "INTEGER", "val": "INTEGER"},
        }
    }
}


def _rels(current_model: str, sql: str):
    scope = qualify_model_sql(sql, "duckdb", _SCHEMA)
    assert scope is not None
    return _relationships_for_model(current_model, scope, _LOOKUP)


class TestBasicJoin:
    def test_simple_join_produces_relationship(self):
        sql = (
            'SELECT m.id, a.name FROM "db"."sch"."m" m '
            'JOIN "db"."sch"."a" a ON m.a_id = a.id'
        )
        rels = _rels("m", sql)
        assert len(rels) == 1
        rel = rels[0]
        assert rel.origin == RelationshipOrigin.lineage
        assert rel.models == ["m", "a"]
        assert rel.condition == '"m"."a_id" = "a"."id"'

    def test_direction_current_model_is_from(self):
        # Current model is `m`; join condition is a.id = m.a_id (right side is m)
        sql = 'SELECT m.id FROM "db"."sch"."a" a JOIN "db"."sch"."m" m ON a.id = m.a_id'
        rels = _rels("m", sql)
        assert len(rels) == 1
        assert rels[0].models[0] == "m"
        assert rels[0].models[1] == "a"

    def test_upstream_only_join_skipped(self):
        # Current model is `m` but the JOIN is between two upstream models
        sql = (
            'SELECT a.id, b.id AS bid FROM "db"."sch"."a" a '
            'JOIN "db"."sch"."b" b ON a.id = b.a_id'
        )
        rels = _rels("m", sql)
        assert rels == []

    def test_self_join_skipped(self):
        sql = (
            'SELECT m1.id FROM "db"."sch"."m" m1 '
            'JOIN "db"."sch"."m" m2 ON m1.id = m2.a_id'
        )
        rels = _rels("m", sql)
        assert rels == []

    def test_no_joins_no_output(self):
        sql = 'SELECT a_id FROM "db"."sch"."m"'
        rels = _rels("m", sql)
        assert rels == []

    def test_origin_is_lineage(self):
        sql = 'SELECT m.id FROM "db"."sch"."m" m JOIN "db"."sch"."a" a ON m.a_id = a.id'
        rels = _rels("m", sql)
        assert all(r.origin == "lineage" for r in rels)


class TestCteResolution:
    def test_cte_chain_resolution(self):
        sql = (
            'WITH a_wrap AS (SELECT id FROM "db"."sch"."a") '
            'SELECT m.id FROM "db"."sch"."m" m '
            "JOIN a_wrap ON m.a_id = a_wrap.id"
        )
        rels = _rels("m", sql)
        assert len(rels) == 1
        assert rels[0].models == ["m", "a"]


class TestOnClauseFiltering:
    def test_non_eq_on_clause_skipped(self):
        sql = 'SELECT m.id FROM "db"."sch"."m" m JOIN "db"."sch"."a" a ON m.a_id > a.id'
        rels = _rels("m", sql)
        assert rels == []


class TestExtractJoinRelationships:
    def test_jaffle_shop_produces_no_self_joins(self):
        """Jaffle shop's models join upstreams only — no current-model joins."""
        man = load_manifest(MANIFEST)
        cat = load_catalog(CATALOG)
        rels = extract_join_relationships(man, cat)
        assert rels == []

    def test_incremental_style_model_produces_relationship(self, tmp_path):
        """Synthesize a model that references itself in a JOIN — should emit a rel."""
        data = json.loads(MANIFEST.read_text())
        orders_uid = "model.jaffle_shop.orders"
        orders_node = data["nodes"][orders_uid]
        orders_node["compiled_code"] = (
            'SELECT o.order_id FROM "jaffle_shop"."main"."orders" o '
            'JOIN "jaffle_shop"."main"."customers" c '
            "ON o.customer_id = c.customer_id"
        )
        orders_node["depends_on"]["nodes"] = [
            "model.jaffle_shop.customers",
            "model.jaffle_shop.orders",
        ]

        man = parse_manifest(data)
        cat = load_catalog(CATALOG)
        rels = extract_join_relationships(man, cat)
        names = [r.name for r in rels]
        assert "orders_customer_id_customers_customer_id" in names
        rel = next(
            r for r in rels if r.name == "orders_customer_id_customers_customer_id"
        )
        assert rel.origin == RelationshipOrigin.lineage
        assert rel.models == ["orders", "customers"]

    def test_deduplication_within_model(self):
        """Multiple joins to the same pair in one model's SQL → one rel."""
        sql = (
            'SELECT m.id FROM "db"."sch"."m" m '
            'JOIN "db"."sch"."a" a1 ON m.a_id = a1.id '
            'JOIN "db"."sch"."a" a2 ON m.a_id = a2.id'
        )
        rels = _rels("m", sql)
        assert len(rels) == 1
