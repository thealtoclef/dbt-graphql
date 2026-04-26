"""Cache-key derivation: pure functions, no fixtures needed.

These tests pin the *exact* equivalence classes that the cache depends on.
A regression here is a correctness regression — different tenants sharing
keys, or identical queries failing to share keys. Both are silent failures
in production.
"""

from __future__ import annotations

from graphql import parse
from sqlalchemy import Column, Integer, MetaData, String, Table, select

from dbt_graphql.api.security import JWTPayload
from dbt_graphql.cache.keys import (
    canonicalize_doc,
    compiled_plan_key,
    doc_subtree_hash,
    hash_sql,
    jwt_signature,
    parsed_doc_key,
)


# ---------------------------------------------------------------------------
# L1 — canonicalization
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_whitespace_variants_collapse(self):
        a = "{ customers { id name } }"
        b = "{\n  customers   {\n    id\n    name\n  }\n}"
        assert canonicalize_doc(a) == canonicalize_doc(b)
        assert parsed_doc_key(a) == parsed_doc_key(b)

    def test_field_order_does_not_collapse(self):
        # GraphQL field order is semantically significant for response
        # ordering, so two different orderings MUST produce different keys.
        a = "{ customers { id name } }"
        b = "{ customers { name id } }"
        assert parsed_doc_key(a) != parsed_doc_key(b)

    def test_invalid_query_falls_back_to_raw(self):
        # An invalid query must still yield a stable key so that we can
        # cache the failure (and not re-parse it on every retry).
        bad = "{ not valid {{{"
        assert canonicalize_doc(bad) == bad
        assert parsed_doc_key(bad).startswith("parse:")

    def test_distinct_queries_distinct_keys(self):
        assert parsed_doc_key("{ a }") != parsed_doc_key("{ b }")


# ---------------------------------------------------------------------------
# L2 — compiled-plan key
# ---------------------------------------------------------------------------


def _root_field(query: str):
    doc = parse(query)
    return doc.definitions[0].selection_set.selections[0]


class TestCompiledPlanKey:
    def test_same_inputs_same_key(self):
        f = _root_field("{ customers { id } }")
        sig = jwt_signature(JWTPayload({"sub": "u1"}))
        k1 = compiled_plan_key(
            field_node=f,
            table_name="customers",
            where=None,
            limit=None,
            offset=None,
            dialect="postgresql",
            jwt_sig=sig,
        )
        k2 = compiled_plan_key(
            field_node=f,
            table_name="customers",
            where=None,
            limit=None,
            offset=None,
            dialect="postgresql",
            jwt_sig=sig,
        )
        assert k1 == k2

    def test_different_dialect_different_key(self):
        f = _root_field("{ customers { id } }")
        sig = jwt_signature(JWTPayload({"sub": "u1"}))
        common = dict(
            field_node=f,
            table_name="customers",
            where=None,
            limit=None,
            offset=None,
            jwt_sig=sig,
        )
        assert (
            compiled_plan_key(dialect="postgresql", **common)
            != compiled_plan_key(dialect="mysql", **common)
        )

    def test_different_where_different_key(self):
        f = _root_field("{ customers { id } }")
        sig = jwt_signature(JWTPayload({"sub": "u1"}))
        common = dict(
            field_node=f,
            table_name="customers",
            limit=None,
            offset=None,
            dialect="postgresql",
            jwt_sig=sig,
        )
        assert (
            compiled_plan_key(where={"id": 1}, **common)
            != compiled_plan_key(where={"id": 2}, **common)
        )

    def test_different_limit_different_key(self):
        f = _root_field("{ customers { id } }")
        sig = jwt_signature(JWTPayload({"sub": "u1"}))
        common = dict(
            field_node=f,
            table_name="customers",
            where=None,
            offset=None,
            dialect="postgresql",
            jwt_sig=sig,
        )
        assert (
            compiled_plan_key(limit=10, **common)
            != compiled_plan_key(limit=20, **common)
        )

    def test_different_jwt_different_key(self):
        f = _root_field("{ customers { id } }")
        common = dict(
            field_node=f,
            table_name="customers",
            where=None,
            limit=None,
            offset=None,
            dialect="postgresql",
        )
        s1 = jwt_signature(JWTPayload({"sub": "alice"}))
        s2 = jwt_signature(JWTPayload({"sub": "bob"}))
        assert (
            compiled_plan_key(jwt_sig=s1, **common)
            != compiled_plan_key(jwt_sig=s2, **common)
        )

    def test_doc_subtree_hash_stable(self):
        # Two parses of the same string yield the same subtree hash.
        f1 = _root_field("{ customers { id name } }")
        f2 = _root_field("{ customers { id name } }")
        assert doc_subtree_hash(f1) == doc_subtree_hash(f2)


class TestJwtSignature:
    def test_identical_payload_identical_sig(self):
        a = JWTPayload({"sub": "u1", "role": "viewer"})
        b = JWTPayload({"sub": "u1", "role": "viewer"})
        assert jwt_signature(a) == jwt_signature(b)

    def test_key_order_independent(self):
        # JSON canonicalization sorts keys — two payloads with same data
        # must produce the same signature regardless of insertion order.
        a = JWTPayload({"sub": "u1", "role": "viewer"})
        b = JWTPayload({"role": "viewer", "sub": "u1"})
        assert jwt_signature(a) == jwt_signature(b)

    def test_nested_payload(self):
        a = JWTPayload({"sub": "u1", "claims": {"org": 7, "tier": "pro"}})
        b = JWTPayload({"sub": "u1", "claims": {"tier": "pro", "org": 7}})
        assert jwt_signature(a) == jwt_signature(b)

    def test_nested_value_change_changes_sig(self):
        a = JWTPayload({"claims": {"org": 7}})
        b = JWTPayload({"claims": {"org": 8}})
        assert jwt_signature(a) != jwt_signature(b)


# ---------------------------------------------------------------------------
# L3 — hash_sql
# ---------------------------------------------------------------------------


class TestHashSql:
    def _stmt(self, name="alice"):
        meta = MetaData()
        t = Table(
            "users",
            meta,
            Column("id", Integer),
            Column("name", String),
        )
        return select(t.c.id).where(t.c.name == name)

    def test_identical_statements_identical_hash(self):
        a = hash_sql(self._stmt("alice"), "postgresql")
        b = hash_sql(self._stmt("alice"), "postgresql")
        assert a == b

    def test_different_bound_params_different_hash(self):
        # The whole point of L3 — two queries with the same SQL shape but
        # different bind values must produce different cache entries.
        a = hash_sql(self._stmt("alice"), "postgresql")
        b = hash_sql(self._stmt("bob"), "postgresql")
        assert a != b

    def test_different_dialects_different_hash(self):
        a = hash_sql(self._stmt(), "postgresql")
        b = hash_sql(self._stmt(), "mysql")
        assert a != b

    def test_unknown_dialect_falls_back_safely(self):
        # Should not raise; falls back to ``str(stmt)``.
        h = hash_sql(self._stmt(), "doris")
        assert isinstance(h, str) and h.startswith("sql:")
