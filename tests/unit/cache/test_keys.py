"""Cache-key derivation: pure function, no fixtures needed.

This is the *whole* tenant-isolation contract. A regression here is a
correctness regression — different tenants sharing keys, or identical
queries failing to share keys. Both are silent failures in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select

from dbt_graphql.cache.keys import hash_sql


def _stmt(name="alice"):
    meta = MetaData()
    t = Table(
        "users",
        meta,
        Column("id", Integer),
        Column("name", String),
    )
    return select(t.c.id).where(t.c.name == name)


class TestHashSql:
    def test_identical_statements_identical_hash(self):
        # Two independently-built statements with the same shape and bind
        # values must produce the same key. This is the "same-tenant
        # repeat → cache hit" foundation.
        a = hash_sql(_stmt("alice"), "postgresql")
        b = hash_sql(_stmt("alice"), "postgresql")
        assert a == b

    def test_different_bound_params_different_hash(self):
        # The cross-tenant correctness foundation: different row-filter
        # bind values → different keys. No cache leak between tenants
        # whose row-filters resolve to different :param values.
        a = hash_sql(_stmt("alice"), "postgresql")
        b = hash_sql(_stmt("bob"), "postgresql")
        assert a != b

    def test_different_dialects_different_hash(self):
        # Same logical query, different dialects → different SQL syntax.
        # A shared Redis must never serve a Postgres entry to a MySQL
        # replica or vice versa.
        a = hash_sql(_stmt(), "postgresql")
        b = hash_sql(_stmt(), "mysql")
        assert a != b

    def test_unknown_dialect_raises(self):
        # We refuse to emit a key rather than silently emit an unsafe
        # one. The previous fallback to ``str(stmt)`` lost bound
        # parameter values from the key — two queries with different
        # bind values would have collided. Cross-tenant leak.
        with pytest.raises(ValueError, match="cannot resolve SQLAlchemy dialect"):
            hash_sql(_stmt(), "doris")

    def test_other_sa_dialects_isolate_params(self):
        # Any dialect SA can load (e.g., sqlite) must still encode bind
        # values into the key. Pins the regression that motivated
        # rejecting unknown dialects: silent param loss = silent leak.
        a = hash_sql(_stmt("alice"), "sqlite")
        b = hash_sql(_stmt("bob"), "sqlite")
        assert a != b

    def test_different_limit_different_hash(self):
        meta = MetaData()
        t = Table("u", meta, Column("id", Integer))
        a = hash_sql(select(t.c.id).limit(10), "postgresql")
        b = hash_sql(select(t.c.id).limit(20), "postgresql")
        assert a != b

    def test_different_column_order_different_hash(self):
        # GraphQL field order is semantically significant for response
        # shape, so two SELECTs with the same columns in different order
        # must NOT collapse to the same cache entry.
        meta = MetaData()
        t = Table("u", meta, Column("id", Integer), Column("name", String))
        a = hash_sql(select(t.c.id, t.c.name), "postgresql")
        b = hash_sql(select(t.c.name, t.c.id), "postgresql")
        assert a != b
