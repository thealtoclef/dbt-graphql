"""End-to-end compiler tests across PostgreSQL and MySQL.

For each adapter the test:
1. Builds the jaffle-shop dbt project (session-scoped fixture)
2. Runs extract_project → format_graphql → parse_db_graphql → compile_query
3. Executes the compiled SQL against the real database
4. Asserts results
"""

from __future__ import annotations

import pytest

from dbt_graphql.compiler.query import compile_query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _field_node(name, selections=None):
    class FN:
        def __init__(self, n, sels=None):
            self.name = type("N", (), {"value": n})()
            self.selection_set = None
            if sels is not None:
                self.selection_set = type("SS", (), {"selections": sels})()

    return FN(name, selections)


def _relation_field_node(col_name, child_names):
    children = [_field_node(n) for n in child_names]
    return type(
        "FN",
        (),
        {
            "name": type("N", (), {"value": col_name})(),
            "selection_set": type("SS", (), {"selections": children})(),
        },
    )()


# ---------------------------------------------------------------------------
# E2E tests (all adapters)
# ---------------------------------------------------------------------------


class TestE2E:
    @pytest.mark.asyncio
    async def test_select_customers(self, adapter_env):
        fn = _field_node(
            "customers",
            [
                _field_node("customer_id"),
                _field_node("first_name"),
                _field_node("last_name"),
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(adapter_env.registry["customers"], [fn], adapter_env.registry)
        )
        assert len(rows) > 0
        assert rows[0]["customer_id"] is not None
        assert "first_name" in rows[0]

    @pytest.mark.asyncio
    async def test_where_filter(self, adapter_env):
        fn = _field_node(
            "customers",
            [_field_node("customer_id"), _field_node("first_name")],
        )
        stmt = compile_query(
            adapter_env.registry["customers"],
            [fn],
            adapter_env.registry,
            where={"customer_id": {"_eq": 1}},
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    @pytest.mark.asyncio
    async def test_limit(self, adapter_env):
        fn = _field_node("customers", [_field_node("customer_id")])
        rows = await adapter_env.db.execute(
            compile_query(
                adapter_env.registry["customers"], [fn], adapter_env.registry, limit=1
            )
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_orders_with_relation(self, adapter_env):
        fn = _field_node(
            "orders",
            [
                _field_node("order_id"),
                _field_node("status"),
                _relation_field_node("customer_id", ["customer_id", "first_name"]),
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(adapter_env.registry["orders"], [fn], adapter_env.registry)
        )
        assert len(rows) > 0
        assert rows[0]["order_id"] is not None
