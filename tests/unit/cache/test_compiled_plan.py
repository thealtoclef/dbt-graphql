"""L2 compiled-plan cache.

Verifies the contract from `docs/plans/sec-j-caching.md` §3.4 — the L2
key isolates tenants without exploding cardinality. We use a real
``compile_query`` against an in-memory ``TableRegistry`` so the cached
value is a real SQLAlchemy ``Select`` — not a mock. The compiler's call
count is asserted via a counter wrapper.
"""

from __future__ import annotations

import pytest
from graphql import parse

from dbt_graphql.api.security import JWTPayload
from dbt_graphql.cache import stats
from dbt_graphql.cache.compiled_plan import compile_with_cache
from dbt_graphql.compiler.query import compile_query
from dbt_graphql.formatter.schema import (
    ColumnDef,
    TableDef,
    TableRegistry,
)


def _make_registry() -> TableRegistry:
    customers = TableDef(
        name="customers",
        database="mydb",
        schema="main",
        table="customers",
        columns=[
            ColumnDef(name="customer_id", gql_type="Integer", not_null=True, is_pk=True),
            ColumnDef(name="first_name", gql_type="Text"),
            ColumnDef(name="last_name", gql_type="Text"),
        ],
    )
    return TableRegistry([customers])


@pytest.fixture
def registry():
    return _make_registry()


def _root_field(query: str):
    return parse(query).definitions[0].selection_set.selections[0]


class _Counter:
    """Wraps the real compile_query, counts invocations."""

    def __init__(self, registry, dialect="postgresql"):
        self.calls = 0
        self.registry = registry
        self.dialect = dialect

    def thunk(self, fnode, table_name, **kwargs):
        tdef = self.registry.get(table_name)

        def _compile():
            self.calls += 1
            return compile_query(
                tdef=tdef,
                field_nodes=[fnode],
                registry=self.registry,
                dialect=self.dialect,
                **kwargs,
            )

        return _compile


@pytest.mark.asyncio
async def test_repeat_same_inputs_cached(fresh_cache, registry):
    fnode = _root_field("{ customers { customer_id first_name } }")
    counter = _Counter(registry)
    common = dict(
        field_node=fnode,
        table_name="customers",
        where=None,
        limit=None,
        offset=None,
        dialect="postgresql",
        jwt_payload=JWTPayload({"sub": "u1"}),
    )

    a = await compile_with_cache(
        compiler=counter.thunk(fnode, "customers"), **common
    )
    b = await compile_with_cache(
        compiler=counter.thunk(fnode, "customers"), **common
    )
    assert counter.calls == 1
    assert str(a) == str(b)
    assert stats.compiled_plan.hit == 1
    assert stats.compiled_plan.miss == 1


@pytest.mark.asyncio
async def test_different_dialects_different_entries(fresh_cache, registry):
    fnode = _root_field("{ customers { customer_id } }")
    counter_pg = _Counter(registry, "postgresql")
    counter_my = _Counter(registry, "mysql")

    await compile_with_cache(
        field_node=fnode,
        table_name="customers",
        where=None,
        limit=None,
        offset=None,
        dialect="postgresql",
        jwt_payload=JWTPayload({"sub": "u1"}),
        compiler=counter_pg.thunk(fnode, "customers"),
    )
    await compile_with_cache(
        field_node=fnode,
        table_name="customers",
        where=None,
        limit=None,
        offset=None,
        dialect="mysql",
        jwt_payload=JWTPayload({"sub": "u1"}),
        compiler=counter_my.thunk(fnode, "customers"),
    )
    assert counter_pg.calls == 1
    assert counter_my.calls == 1


@pytest.mark.asyncio
async def test_distinct_users_distinct_entries(fresh_cache, registry):
    """Two users with different JWTs must NOT share an L2 entry.

    This pins the multi-tenant correctness invariant — even when the
    compiled SQL would be identical, mixing compiled plans across users
    risks leaking row-filter bind values across tenants.
    """
    fnode = _root_field("{ customers { customer_id } }")
    counter = _Counter(registry)
    common = dict(
        field_node=fnode,
        table_name="customers",
        where=None,
        limit=None,
        offset=None,
        dialect="postgresql",
    )

    await compile_with_cache(
        compiler=counter.thunk(fnode, "customers"),
        jwt_payload=JWTPayload({"sub": "alice"}),
        **common,
    )
    await compile_with_cache(
        compiler=counter.thunk(fnode, "customers"),
        jwt_payload=JWTPayload({"sub": "bob"}),
        **common,
    )
    assert counter.calls == 2


@pytest.mark.asyncio
async def test_identical_payloads_share_entry(fresh_cache, registry):
    """Two structurally-identical JWT payloads share an entry.

    Same data → same signature → same cache key. This is the
    cross-session sharing benefit.
    """
    fnode = _root_field("{ customers { customer_id } }")
    counter = _Counter(registry)
    common = dict(
        field_node=fnode,
        table_name="customers",
        where=None,
        limit=None,
        offset=None,
        dialect="postgresql",
    )

    await compile_with_cache(
        jwt_payload=JWTPayload({"role": "viewer", "tenant": "x"}),
        compiler=counter.thunk(fnode, "customers"),
        **common,
    )
    await compile_with_cache(
        jwt_payload=JWTPayload({"role": "viewer", "tenant": "x"}),
        compiler=counter.thunk(fnode, "customers"),
        **common,
    )
    assert counter.calls == 1


@pytest.mark.asyncio
async def test_where_args_isolate(fresh_cache, registry):
    fnode = _root_field("{ customers { customer_id } }")
    counter = _Counter(registry)
    common = dict(
        field_node=fnode,
        table_name="customers",
        limit=None,
        offset=None,
        dialect="postgresql",
        jwt_payload=JWTPayload({}),
    )

    await compile_with_cache(
        where={"customer_id": 1},
        compiler=counter.thunk(fnode, "customers", where={"customer_id": 1}),
        **common,
    )
    await compile_with_cache(
        where={"customer_id": 2},
        compiler=counter.thunk(fnode, "customers", where={"customer_id": 2}),
        **common,
    )
    assert counter.calls == 2
