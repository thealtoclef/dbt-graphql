"""Resolver-layer behavior for the inline aggregate envelope.

Verifies the lazy-batching invariant: regardless of how many aggregate
fields a client selects on ``{T}Result``, only **one** DB round-trip
fires per request — the first resolver computes all aggregates and
parks the result on the carrier dict, siblings await the same future.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dbt_graphql.cache import CacheConfig
from dbt_graphql.formatter.schema import ColumnDef, TableDef
from dbt_graphql.graphql.resolvers import _make_aggregate_field_resolver


def _tdef() -> TableDef:
    return TableDef(
        name="Invoice",
        database="mydb",
        schema="main",
        table="Invoice",
        columns=[
            ColumnDef(name="InvoiceId", gql_type="Int", not_null=True, is_pk=True),
            ColumnDef(name="Total", gql_type="Float"),
        ],
    )


def _make_info(db) -> MagicMock:
    info = MagicMock()
    info.context = {
        "registry": MagicMock(),
        "db": db,
        "jwt_payload": {},
        "policy_engine": None,
        "cache_config": CacheConfig(),
    }
    info.field_nodes = []
    return info


@pytest.mark.asyncio
async def test_single_round_trip_for_multiple_agg_fields(monkeypatch, fresh_cache):
    """Selecting 3 aggregate fields fires ``compile_aggregate_query`` once
    and ``db.execute`` once — the result is shared via the carrier dict."""
    del fresh_cache

    compile_calls = 0

    def fake_compile(**_):
        nonlocal compile_calls
        compile_calls += 1
        return MagicMock(name="stmt")

    monkeypatch.setattr(
        "dbt_graphql.graphql.resolvers.compile_aggregate_query", fake_compile
    )

    execute_calls = 0
    fake_row = {"count": 42, "sum_Total": 1000.0, "avg_Total": 23.8}

    async def fake_execute(_stmt):
        nonlocal execute_calls
        execute_calls += 1
        return [fake_row]

    db = MagicMock()
    db.dialect_name = "postgresql"
    db.execute = fake_execute

    tdef = _tdef()
    info = _make_info(db)
    parent: dict = {"where": None}

    # Three sibling aggregate field resolvers — each is its own factory.
    r_count = _make_aggregate_field_resolver("count", tdef)
    r_sum = _make_aggregate_field_resolver("sum_Total", tdef)
    r_avg = _make_aggregate_field_resolver("avg_Total", tdef)

    # Run them sequentially in the same event loop as ariadne would.
    assert await r_count(parent, info) == 42
    assert await r_sum(parent, info) == 1000.0
    assert await r_avg(parent, info) == 23.8

    assert compile_calls == 1, "expected exactly one compile per request"
    assert execute_calls == 1, "expected exactly one DB round-trip per request"


@pytest.mark.asyncio
async def test_sibling_resolvers_see_translated_error(monkeypatch, fresh_cache):
    """If the first agg resolver fails, sibling awaiters get the same
    GraphQLError — not a raw PolicyError / SAPoolTimeoutError."""
    del fresh_cache
    from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

    from dbt_graphql.config import PoolConfig
    from dbt_graphql.graphql.resolvers import POOL_TIMEOUT_CODE
    from graphql import GraphQLError

    monkeypatch.setattr(
        "dbt_graphql.graphql.resolvers.compile_aggregate_query",
        lambda **_: MagicMock(name="stmt"),
    )

    async def boom(_stmt):
        raise SAPoolTimeoutError("pool", None, None)

    db = MagicMock()
    db.dialect_name = "postgresql"
    db.execute = boom
    db._pool = PoolConfig(size=1, max_overflow=0, timeout=1, recycle=1, retry_after=2)

    tdef = _tdef()
    info = _make_info(db)
    parent: dict = {"where": None}

    r_count = _make_aggregate_field_resolver("count", tdef)
    r_sum = _make_aggregate_field_resolver("sum_Total", tdef)

    with pytest.raises(GraphQLError) as first_exc:
        await r_count(parent, info)
    assert first_exc.value.extensions["code"] == POOL_TIMEOUT_CODE

    # The sibling sees the same translated GraphQLError, not the raw
    # SAPoolTimeoutError that originally fired.
    with pytest.raises(GraphQLError) as second_exc:
        await r_sum(parent, info)
    assert second_exc.value.extensions["code"] == POOL_TIMEOUT_CODE
