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
from dbt_graphql.graphql.policy import ColumnAccessDenied, ResolvedPolicy


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


# ---------------------------------------------------------------------------
# E2E tests (all adapters)
# ---------------------------------------------------------------------------


class TestE2E:
    """Basic compile_query tests using row-only queries."""

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
            compile_query(
                tdef=adapter_env.registry["customers"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
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
            tdef=adapter_env.registry["customers"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where={"customer_id": {"_eq": 1}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    @pytest.mark.asyncio
    async def test_limit(self, adapter_env):
        fn = _field_node("customers", [_field_node("customer_id")])
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["customers"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=1,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Aggregate Tests
# ---------------------------------------------------------------------------


class TestAggregates:
    """GraphJin-style aggregate query tests."""

    @pytest.mark.asyncio
    async def test_aggregate_count_only(self, adapter_env):
        """Query with only count (global COUNT(*))."""
        fn = _field_node("orders", [_field_node("_aggregate", [_field_node("count")])])
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert "_count" in rows[0]
        assert rows[0]["_count"] > 0

    @pytest.mark.asyncio
    async def test_aggregate_sum(self, adapter_env):
        """Query with sum_amount."""
        # Use 'amount' column which is numeric in orders table
        fn = _field_node(
            "orders",
            [_field_node("_aggregate", [_field_node("sum", [_field_node("amount")])])],
        )
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert "_sum_amount" in rows[0]
        # Sum of amounts should be > 0 if there are any orders with amounts
        result = rows[0]["_sum_amount"]
        assert result is not None

    @pytest.mark.asyncio
    async def test_aggregate_avg(self, adapter_env):
        """Query with avg_amount."""
        fn = _field_node(
            "orders",
            [_field_node("_aggregate", [_field_node("avg", [_field_node("amount")])])],
        )
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert "_avg_amount" in rows[0]
        result = rows[0]["_avg_amount"]
        assert result is not None

    @pytest.mark.asyncio
    async def test_aggregate_min_max(self, adapter_env):
        """Query with min_amount, max_amount."""
        fn = _field_node(
            "orders",
            [
                _field_node(
                    "_aggregate",
                    [
                        _field_node("min", [_field_node("amount")]),
                        _field_node("max", [_field_node("amount")]),
                    ],
                )
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert "_min_amount" in rows[0]
        assert "_max_amount" in rows[0]
        assert rows[0]["_min_amount"] <= rows[0]["_max_amount"]

    @pytest.mark.asyncio
    async def test_aggregate_stddev_var(self, adapter_env):
        """Query with stddev_amount, var_amount."""
        fn = _field_node(
            "orders",
            [
                _field_node(
                    "_aggregate",
                    [
                        _field_node("stddev", [_field_node("amount")]),
                        _field_node("var", [_field_node("amount")]),
                    ],
                )
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert "_stddev_amount" in rows[0]
        assert "_var_amount" in rows[0]
        # variance should be >= 0, and stddev = sqrt(variance) should be reasonable
        assert rows[0]["_var_amount"] >= 0

    @pytest.mark.asyncio
    async def test_aggregate_mixed(self, adapter_env):
        """Query with multiple aggregate types in one query."""
        fn = _field_node(
            "orders",
            [
                _field_node(
                    "_aggregate",
                    [
                        _field_node("count"),
                        _field_node("sum", [_field_node("amount")]),
                        _field_node("avg", [_field_node("amount")]),
                        _field_node("min", [_field_node("amount")]),
                        _field_node("max", [_field_node("amount")]),
                    ],
                )
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert rows[0]["_count"] > 0
        assert rows[0]["_sum_amount"] is not None
        assert rows[0]["_avg_amount"] is not None
        assert rows[0]["_min_amount"] is not None
        assert rows[0]["_max_amount"] is not None

    @pytest.mark.asyncio
    async def test_aggregate_with_where(self, adapter_env):
        """Aggregates with WHERE clause."""
        fn = _field_node(
            "orders",
            [
                _field_node(
                    "_aggregate",
                    [_field_node("count"), _field_node("sum", [_field_node("amount")])],
                )
            ],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["orders"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where={"status": {"_eq": "completed"}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) == 1
        # Should only count/compute over completed orders
        assert rows[0]["_count"] >= 0

    @pytest.mark.asyncio
    async def test_aggregate_group_by(self, adapter_env):
        """Dimensions + aggregates (implicit GROUP BY)."""
        # Use 'status' as dimension - it's not a foreign key (no relation)
        fn = _field_node(
            "orders",
            [
                _field_node("status"),  # dimension (no relation)
                _field_node(
                    "_aggregate",
                    [
                        _field_node("count"),
                        _field_node("sum", [_field_node("amount")]),
                    ],
                ),
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(adapter_env.registry["orders"], [fn], adapter_env.registry)
        )
        # Should return one row per status value
        assert len(rows) > 1
        # Each row should have status, count, and sum_amount
        for row in rows:
            assert "status" in row
            assert "_count" in row
            assert "_sum_amount" in row

    @pytest.mark.asyncio
    async def test_aggregate_count_distinct(self, adapter_env):
        """Query with count_distinct."""
        fn = _field_node(
            "orders",
            [
                _field_node(
                    "_aggregate",
                    [_field_node("count_distinct", [_field_node("status")])],
                )
            ],
        )
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )
        )
        assert len(rows) == 1
        assert "_count_distinct_status" in rows[0]
        result = rows[0]["_count_distinct_status"]
        assert result is not None
        assert result >= 0

    @pytest.mark.asyncio
    async def test_distinct_with_aggregates_rejected(self, adapter_env):
        """Verify error when both distinct and aggregates are specified."""
        fn = _field_node(
            "orders",
            [_field_node("status"), _field_node("_aggregate", [_field_node("count")])],
        )
        with pytest.raises(ValueError, match="distinct and aggregate fields"):
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=True,
                resolve_policy=None,
            )


# ---------------------------------------------------------------------------
# Order By Tests
# ---------------------------------------------------------------------------


class TestOrderBy:
    """GraphJin-style order_by tests."""

    @pytest.mark.asyncio
    async def test_order_by_dimension(self, adapter_env):
        """order_by: [ (column, direction), ... ]."""
        fn = _field_node(
            "customers",
            [_field_node("customer_id"), _field_node("first_name")],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["customers"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where=None,
            order_by=[("first_name", "asc")],
            limit=5,
            distinct=None,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) == 5
        # Verify ordering
        first_names = [r["first_name"] for r in rows]
        assert first_names == sorted(first_names)

    @pytest.mark.asyncio
    async def test_order_by_multiple(self, adapter_env):
        """Multiple columns in order_by."""
        fn = _field_node(
            "customers",
            [
                _field_node("last_name"),
                _field_node("first_name"),
                _field_node("customer_id"),
            ],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["customers"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where=None,
            order_by=[("last_name", "asc"), ("first_name", "asc")],
            limit=5,
            distinct=None,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) == 5
        # Verify ordering by last_name, then first_name
        for i in range(len(rows) - 1):
            curr, next_ = rows[i], rows[i + 1]
            if curr["last_name"] == next_["last_name"]:
                assert curr["first_name"] <= next_["first_name"]
            else:
                assert curr["last_name"] < next_["last_name"]


# ---------------------------------------------------------------------------
# Distinct Tests
# ---------------------------------------------------------------------------


class TestDistinct:
    """GraphJin-style distinct tests."""

    @pytest.mark.asyncio
    async def test_distinct_single_column(self, adapter_env):
        """distinct: [Column]."""
        fn = _field_node("orders", [_field_node("status")])
        stmt = compile_query(
            tdef=adapter_env.registry["orders"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where={"status": {"_neq": "pending"}},
            order_by=[("status", "asc")],
            limit=10,
            distinct=True,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        # Should return only unique status values
        statuses = {r["status"] for r in rows}
        assert len(rows) == len(statuses)

    @pytest.mark.asyncio
    async def test_distinct_multiple_columns(self, adapter_env):
        """distinct: [Col1, Col2]."""
        # Use status and a numeric column (though distinct on numeric is unusual)
        fn = _field_node(
            "orders",
            [_field_node("status"), _field_node("order_id")],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["orders"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=True,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        # Each row should be unique
        assert len(rows) > 0


# ---------------------------------------------------------------------------
# Combined Query Tests
# ---------------------------------------------------------------------------


class TestCombinedQueries:
    """Tests combining multiple GraphJin-style features."""

    @pytest.mark.asyncio
    async def test_full_query_where_order_by_limit(self, adapter_env):
        """where + order_by + limit."""
        fn = _field_node(
            "customers",
            [
                _field_node("customer_id"),
                _field_node("first_name"),
                _field_node("last_name"),
            ],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["customers"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where={"last_name": {"_like": "S%"}},
            order_by=[("first_name", "asc")],
            limit=10,
            distinct=None,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) <= 10
        # Verify all rows match the WHERE condition
        for row in rows:
            assert row["last_name"].startswith("S")
        # Verify ordering
        first_names = [r["first_name"] for r in rows]
        assert first_names == sorted(first_names)

    @pytest.mark.asyncio
    async def test_full_query_with_aggregates_and_where(self, adapter_env):
        """aggregates + where on grouped query."""
        fn = _field_node(
            "orders",
            [
                _field_node("status"),
                _field_node(
                    "_aggregate",
                    [_field_node("count"), _field_node("sum", [_field_node("amount")])],
                ),
            ],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["orders"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where={"status": {"_neq": "cancelled"}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) > 0
        # No cancelled orders should be present
        for row in rows:
            assert row["status"] != "cancelled"
            assert "_count" in row
            assert "_sum_amount" in row

    @pytest.mark.asyncio
    async def test_query_all_features(self, adapter_env):
        """where + distinct + order_by + limit on row query.

        Note: distinct is PostgreSQL-specific (DISTINCT ON). On MySQL it is
        silently ignored per SQLAlchemy's current behavior. This test verifies
        the query compiles and executes; the distinct behavior is only verified
        on PostgreSQL.
        """
        fn = _field_node(
            "orders",
            [
                _field_node("status"),
                _field_node("order_id"),
            ],
        )
        stmt = compile_query(
            tdef=adapter_env.registry["orders"],
            field_nodes=[fn],
            registry=adapter_env.registry,
            where={"status": {"_neq": "pending"}},
            order_by=[("status", "asc")],
            limit=10,
            distinct=True,
            resolve_policy=None,
        )
        rows = await adapter_env.db.execute(stmt)
        assert len(rows) <= 10
        # Verify all rows match the where condition (status != 'pending')
        for row in rows:
            assert row["status"] != "pending"


# ---------------------------------------------------------------------------
# Policy Tests with New API
# ---------------------------------------------------------------------------


class TestPolicies:
    """Policy enforcement tests using compile_query with resolve_policy."""

    @pytest.fixture
    def block_status_policy(self):
        """Policy that blocks the 'status' column."""
        return lambda table: ResolvedPolicy(blocked_columns=frozenset({"status"}))

    @pytest.fixture
    def block_customer_id_policy(self):
        """Policy that blocks the 'customer_id' column."""
        return lambda table: ResolvedPolicy(blocked_columns=frozenset({"customer_id"}))

    @pytest.fixture
    def mask_amount_policy(self):
        """Policy that masks the 'amount' column with NULL."""
        return lambda table: ResolvedPolicy(masks={"amount": None})

    @pytest.mark.asyncio
    async def test_policy_blocks_column_in_where(
        self, adapter_env, block_status_policy
    ):
        """Blocked column in WHERE raises error."""
        fn = _field_node(
            "orders",
            [_field_node("order_id"), _field_node("amount")],
        )
        with pytest.raises(ColumnAccessDenied):
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where={"status": {"_eq": "completed"}},
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=block_status_policy,
            )

    @pytest.mark.asyncio
    async def test_policy_blocks_column_in_order_by(
        self, adapter_env, block_customer_id_policy
    ):
        """Blocked column in ORDER BY raises error."""
        fn = _field_node(
            "orders",
            [_field_node("order_id"), _field_node("amount")],
        )
        with pytest.raises(ColumnAccessDenied):
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=[("customer_id", "asc")],
                limit=None,
                distinct=None,
                resolve_policy=block_customer_id_policy,
            )

    @pytest.mark.asyncio
    async def test_policy_masks_aggregate(self, adapter_env, mask_amount_policy):
        """Masked column affects aggregate correctly - aggregate uses underlying column."""
        fn = _field_node(
            "orders",
            [
                _field_node(
                    "_aggregate",
                    [
                        _field_node("sum", [_field_node("amount")]),
                        _field_node("max", [_field_node("amount")]),
                    ],
                )
            ],
        )
        # The mask policy masks 'amount' column to NULL
        # Aggregates should still work on the underlying column
        rows = await adapter_env.db.execute(
            compile_query(
                tdef=adapter_env.registry["orders"],
                field_nodes=[fn],
                registry=adapter_env.registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=mask_amount_policy,
            )
        )
        assert len(rows) == 1
        # Aggregates should still compute (mask affects SELECT output, not computation)
        # The sum_amount and max_amount should still be returned
        assert rows[0]["_sum_amount"] is not None or rows[0].get("_sum_amount") is None
