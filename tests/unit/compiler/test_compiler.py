"""Tests for the SQL compiler (compiler.py).

Verifies that generated SQL uses the correct dialect-specific functions
when compiled against different SQLAlchemy dialects.
"""

import pytest
from sqlalchemy.dialects import mysql, postgresql

from dbt_graphql.compiler.query import (
    agg_fields_for_table,
    compile_aggregate_query,
    compile_group_query,
    compile_nodes_query,
)
from dbt_graphql.formatter.schema import (
    ColumnDef,
    RelationDef,
    TableDef,
    TableRegistry,
)


def _make_registry() -> tuple[TableDef, TableRegistry]:
    customers = TableDef(
        name="customers",
        database="mydb",
        schema="main",
        table="customers",
        columns=[
            ColumnDef(
                name="customer_id", gql_type="Integer", not_null=True, is_pk=True
            ),
            ColumnDef(name="first_name", gql_type="Text"),
            ColumnDef(name="last_name", gql_type="Text"),
        ],
    )
    orders = TableDef(
        name="orders",
        database="mydb",
        schema="main",
        table="orders",
        columns=[
            ColumnDef(name="order_id", gql_type="Integer", not_null=True, is_pk=True),
            ColumnDef(
                name="customer_id",
                gql_type="Integer",
                not_null=True,
                relation=RelationDef(
                    target_model="customers", target_column="customer_id"
                ),
            ),
            ColumnDef(name="order_date", gql_type="Text"),
            ColumnDef(name="status", gql_type="Text"),
        ],
    )
    registry = TableRegistry([customers, orders])
    return customers, registry


def _field_node(name, selections=None):
    class Sel:
        def __init__(self, name):
            self.name = type("N", (), {"value": name})()

    class FN:
        def __init__(self, name, sels=None):
            self.name = type("N", (), {"value": name})()
            self.selection_set = None
            if sels is not None:
                ss = type("SS", (), {"selections": sels})()
                self.selection_set = ss

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


def _sql(stmt, dialect_mod) -> str:
    return str(
        stmt.compile(
            dialect=dialect_mod.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# ---------------------------------------------------------------------------
# Flat queries
# ---------------------------------------------------------------------------


class TestFlatQuery:
    def test_selects_scalar_columns(self):
        customers, registry = _make_registry()
        fn = _field_node(
            "customers", [_field_node("customer_id"), _field_node("first_name")]
        )
        stmt = compile_nodes_query(customers, [fn], registry)
        sql = _sql(stmt, postgresql)
        assert "customer_id" in sql
        assert "first_name" in sql

    def test_limit(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(customers, [fn], registry, limit=10)
        sql = _sql(stmt, postgresql)
        assert "LIMIT 10" in sql

    def test_offset(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(customers, [fn], registry, limit=10, offset=20)
        sql = _sql(stmt, postgresql)
        assert "OFFSET 20" in sql


class TestWhereFilter:
    def test_equality_filter(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers, [fn], registry, where={"customer_id": {"_eq": 1}}
        )
        sql = _sql(stmt, postgresql)
        assert "WHERE" in sql
        assert "1" in sql

    def test_neq_filter(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers, [fn], registry, where={"customer_id": {"_neq": 1}}
        )
        sql = _sql(stmt, postgresql)
        assert "WHERE" in sql

    def test_in_filter(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers, [fn], registry, where={"customer_id": {"_in": [1, 2, 3]}}
        )
        sql = _sql(stmt, postgresql)
        assert "IN" in sql.upper()

    def test_is_null_filter(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("first_name")])
        stmt = compile_nodes_query(
            customers, [fn], registry, where={"first_name": {"_is_null": True}}
        )
        sql = _sql(stmt, postgresql)
        assert "IS NULL" in sql.upper()

    def test_and_combinator(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers,
            [fn],
            registry,
            where={
                "_and": [{"customer_id": {"_eq": 1}}, {"first_name": {"_eq": "Alice"}}]
            },
        )
        sql = _sql(stmt, postgresql)
        assert "AND" in sql.upper()

    def test_or_combinator(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers,
            [fn],
            registry,
            where={"_or": [{"customer_id": {"_eq": 1}}, {"customer_id": {"_eq": 2}}]},
        )
        sql = _sql(stmt, postgresql)
        assert "OR" in sql.upper()

    def test_not_combinator(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers,
            [fn],
            registry,
            where={"_not": {"customer_id": {"_eq": 1}}},
        )
        sql = _sql(stmt, postgresql)
        # SQLAlchemy may optimize NOT (col = x) → col != x; either form is correct.
        assert "WHERE" in sql and "customer_id" in sql

    def test_empty_where_does_not_raise(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(customers, [fn], registry, where={})
        sql = _sql(stmt, postgresql)
        assert "WHERE" not in sql


class TestOrderBy:
    def test_asc_order(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers, [fn], registry, order_by=[{"customer_id": "asc"}]
        )
        sql = _sql(stmt, postgresql)
        assert "ORDER BY" in sql.upper()
        assert "ASC" in sql.upper()

    def test_desc_order(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_nodes_query(
            customers, [fn], registry, order_by=[{"customer_id": "desc"}]
        )
        sql = _sql(stmt, postgresql)
        assert "DESC" in sql.upper()

    def test_invalid_direction_raises_nulls_variant(self):
        # nulls_first/last variants are PostgreSQL-only and not supported.
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        with pytest.raises(ValueError, match="Unknown order_by"):
            compile_nodes_query(
                customers, [fn], registry, order_by=[{"customer_id": "desc_nulls_last"}]
            )

    def test_multi_column_order(self):
        customers, registry = _make_registry()
        fn = _field_node(
            "customers", [_field_node("customer_id"), _field_node("first_name")]
        )
        stmt = compile_nodes_query(
            customers,
            [fn],
            registry,
            order_by=[{"first_name": "asc"}, {"customer_id": "desc"}],
        )
        sql = _sql(stmt, postgresql)
        assert "first_name" in sql
        assert "customer_id" in sql

    def test_invalid_direction_raises(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        with pytest.raises(ValueError, match="Unknown order_by"):
            compile_nodes_query(
                customers, [fn], registry, order_by=[{"customer_id": "sideways"}]
            )


def _agg_table() -> TableDef:
    """A small Invoice-like table with mixed column types for aggregate testing."""
    return TableDef(
        name="Invoice",
        database="mydb",
        schema="main",
        table="Invoice",
        columns=[
            ColumnDef(name="InvoiceId", gql_type="Int", not_null=True, is_pk=True),
            ColumnDef(name="CustomerId", gql_type="Int"),
            ColumnDef(name="BillingState", gql_type="String"),
            ColumnDef(name="Total", gql_type="Float"),
        ],
    )


class TestAggFieldsForTable:
    def test_count_first(self):
        fields = agg_fields_for_table(_agg_table())
        assert fields[0] == ("count", "Int")

    def test_numeric_columns_get_full_set(self):
        fields = dict(agg_fields_for_table(_agg_table()))
        # Float column gets sum/avg/stddev/var (all Float) + min/max (Float).
        assert fields["sum_Total"] == "Float"
        assert fields["avg_Total"] == "Float"
        assert fields["stddev_Total"] == "Float"
        assert fields["var_Total"] == "Float"
        assert fields["min_Total"] == "Float"
        assert fields["max_Total"] == "Float"
        # Int column: min/max preserve Int.
        assert fields["min_CustomerId"] == "Int"
        assert fields["max_CustomerId"] == "Int"

    def test_non_numeric_only_min_max(self):
        fields = dict(agg_fields_for_table(_agg_table()))
        assert fields["min_BillingState"] == "String"
        assert fields["max_BillingState"] == "String"
        # No sum_/avg_ for String — would be invalid SQL.
        assert "sum_BillingState" not in fields
        assert "avg_BillingState" not in fields


class TestCompileAggregateQuery:
    def test_count_only(self):
        stmt = compile_aggregate_query(_agg_table(), {"count"})
        sql = _sql(stmt, postgresql)
        assert "count(" in sql.lower()
        assert "sum(" not in sql.lower()

    def test_subset_of_aggregates(self):
        stmt = compile_aggregate_query(_agg_table(), {"count", "sum_Total"})
        sql = _sql(stmt, postgresql).lower()
        assert "count(" in sql
        assert "sum(" in sql
        # Unselected aggregates are not projected.
        assert "avg(" not in sql
        assert "min(" not in sql

    def test_unknown_field_silently_skipped(self):
        # Unknown column inside _sum_<col> doesn't crash; it's just dropped.
        stmt = compile_aggregate_query(_agg_table(), {"sum_NotAColumn"})
        sql = _sql(stmt, postgresql).lower()
        # Empty projection set falls back to _count.
        assert "count(" in sql

    def test_with_where(self):
        stmt = compile_aggregate_query(
            _agg_table(),
            {"count"},
            where={"BillingState": {"_eq": "CA"}},
        )
        sql = _sql(stmt, postgresql)
        assert "WHERE" in sql.upper()
        assert "CA" in sql


class TestCompileGroupQuery:
    def _select(self, *names):
        return _field_node("Invoice_group", [_field_node(n) for n in names])

    def test_groups_by_dimension(self):
        stmt = compile_group_query(
            _agg_table(), [self._select("BillingState", "count")]
        )
        sql = _sql(stmt, postgresql).upper()
        assert "GROUP BY" in sql
        assert "BILLINGSTATE" in sql
        assert "COUNT(" in sql

    def test_no_dimension_no_group_by(self):
        # Selecting only aggregates → grand total, no GROUP BY.
        stmt = compile_group_query(_agg_table(), [self._select("count", "sum_Total")])
        sql = _sql(stmt, postgresql).upper()
        assert "GROUP BY" not in sql
        assert "COUNT(" in sql
        assert "SUM(" in sql

    def test_multi_dimension(self):
        stmt = compile_group_query(
            _agg_table(),
            [self._select("BillingState", "CustomerId", "count")],
        )
        sql = _sql(stmt, postgresql).upper()
        assert "GROUP BY" in sql
        assert "BILLINGSTATE" in sql
        assert "CUSTOMERID" in sql

    def test_order_by_aggregate_alias(self):
        stmt = compile_group_query(
            _agg_table(),
            [self._select("BillingState", "count", "sum_Total")],
            order_by=[{"sum_Total": "desc"}],
        )
        sql = _sql(stmt, postgresql).upper()
        assert "ORDER BY" in sql
        assert "SUM_TOTAL" in sql

    def test_order_by_dimension(self):
        stmt = compile_group_query(
            _agg_table(),
            [self._select("BillingState", "count")],
            order_by=[{"BillingState": "asc"}],
        )
        sql = _sql(stmt, postgresql).upper()
        assert "ORDER BY" in sql

    def test_invalid_order_by_direction(self):
        with pytest.raises(ValueError, match="Unknown order_by"):
            compile_group_query(
                _agg_table(),
                [self._select("BillingState", "count")],
                order_by=[{"BillingState": "sideways"}],
            )


def _three_table_registry():
    """addresses ← customers ← orders (2-hop chain)."""
    addresses = TableDef(
        name="addresses",
        database="mydb",
        schema="main",
        table="addresses",
        columns=[
            ColumnDef(name="address_id", gql_type="Int", not_null=True),
            ColumnDef(name="city", gql_type="String"),
        ],
    )
    customers = TableDef(
        name="customers",
        database="mydb",
        schema="main",
        table="customers",
        columns=[
            ColumnDef(name="customer_id", gql_type="Int", not_null=True),
            ColumnDef(
                name="address_id",
                gql_type="Int",
                relation=RelationDef(
                    target_model="addresses", target_column="address_id"
                ),
            ),
        ],
    )
    orders = TableDef(
        name="orders",
        database="mydb",
        schema="main",
        table="orders",
        columns=[
            ColumnDef(name="order_id", gql_type="Int", not_null=True),
            ColumnDef(
                name="customer_id",
                gql_type="Int",
                relation=RelationDef(
                    target_model="customers", target_column="customer_id"
                ),
            ),
        ],
    )
    return orders, TableRegistry([addresses, customers, orders])


class TestMultiHopNesting:
    def test_two_hop_compiles_without_error(self):
        """orders → customers → addresses (2-hop) must produce valid SQL."""
        orders, registry = _three_table_registry()
        # orders { order_id customer_id { customer_id address_id { city } } }
        fn = _field_node(
            "orders",
            [
                _field_node("order_id"),
                _field_node(
                    "customer_id",
                    [
                        _field_node("customer_id"),
                        _field_node("address_id", [_field_node("city")]),
                    ],
                ),
            ],
        )
        stmt = compile_nodes_query(orders, [fn], registry)
        sql = _sql(stmt, postgresql)
        assert "child_1" in sql
        assert "child_2" in sql
        assert "LATERAL" not in sql

    def test_two_hop_contains_nested_json(self):
        """SQL must nest JSON aggregation at both levels."""
        orders, registry = _three_table_registry()
        fn = _field_node(
            "orders",
            [
                _field_node("order_id"),
                _field_node(
                    "customer_id",
                    [
                        _field_node("customer_id"),
                        _field_node("address_id", [_field_node("city")]),
                    ],
                ),
            ],
        )
        stmt = compile_nodes_query(orders, [fn], registry)
        sql = _sql(stmt, postgresql)
        # Both levels must aggregate JSON
        assert sql.count("JSONB_AGG") == 2
        assert sql.count("JSONB_BUILD_OBJECT") == 2

    def test_cycle_detection_raises(self):
        """A → B → A → B must raise ValueError when the query follows the cycle.

        The cycle guard fires when the same model appears twice in the subquery
        stack (visited set). The query must explicitly select the back-edge
        field for the cycle to materialise — just having a cycle in the schema
        does not trigger it.
        """
        a = TableDef(
            name="A",
            database="db",
            schema="s",
            table="a",
            columns=[
                ColumnDef(name="id", gql_type="Int"),
                ColumnDef(
                    name="b_id",
                    gql_type="Int",
                    relation=RelationDef(target_model="B", target_column="id"),
                ),
            ],
        )
        b = TableDef(
            name="B",
            database="db",
            schema="s",
            table="b",
            columns=[
                ColumnDef(name="id", gql_type="Int"),
                ColumnDef(
                    name="a_id",
                    gql_type="Int",
                    relation=RelationDef(target_model="A", target_column="id"),
                ),
            ],
        )
        registry = TableRegistry([a, b])
        # A → B (depth 1) → A (depth 2) → B (depth 3): "B" already in visited → raises
        fn = _field_node(
            "A",
            [
                _field_node("id"),
                _field_node(
                    "b_id",
                    [
                        _field_node("id"),
                        _field_node(
                            "a_id",
                            [
                                _field_node("id"),
                                _field_node("b_id", [_field_node("id")]),
                            ],
                        ),
                    ],
                ),
            ],
        )
        with pytest.raises(ValueError, match="Circular"):
            compile_nodes_query(a, [fn], registry)

    def test_depth_limit_raises_when_max_depth_set(self):
        """compile_nodes_query(max_depth=N) must raise once nesting exceeds N."""
        max_depth = 2
        # Build a linear chain: T0 → T1 → T2 → T3 (3 hops, exceeds max_depth=2)
        tables = []
        for i in range(4):
            cols = [ColumnDef(name="id", gql_type="Int")]
            if i < 3:
                cols.append(
                    ColumnDef(
                        name="next_id",
                        gql_type="Int",
                        relation=RelationDef(
                            target_model=f"T{i + 1}", target_column="id"
                        ),
                    )
                )
            tables.append(
                TableDef(
                    name=f"T{i}", database="db", schema="s", table=f"t{i}", columns=cols
                )
            )
        registry = TableRegistry(tables)

        def chain_node(level):
            if level == 3:
                return _field_node("next_id", [_field_node("id")])
            return _field_node("next_id", [_field_node("id"), chain_node(level + 1)])

        fn = _field_node("T0", [_field_node("id"), chain_node(0)])
        with pytest.raises(ValueError, match="depth"):
            compile_nodes_query(tables[0], [fn], registry, max_depth=max_depth)

    def test_no_depth_limit_by_default(self):
        """Without max_depth, a deep non-cyclic chain compiles without error."""
        orders, registry = _three_table_registry()
        fn = _field_node(
            "orders",
            [
                _field_node("order_id"),
                _field_node(
                    "customer_id",
                    [
                        _field_node("customer_id"),
                        _field_node("address_id", [_field_node("city")]),
                    ],
                ),
            ],
        )
        # Should not raise — no max_depth means unlimited
        stmt = compile_nodes_query(orders, [fn], registry)
        assert stmt is not None


# ---------------------------------------------------------------------------
# Dialect-specific JSON function compilation
# ---------------------------------------------------------------------------


def _relation_sql(dialect_mod):
    _, registry = _make_registry()
    orders = registry["orders"]
    fn = _field_node(
        "orders",
        [
            _field_node("order_id"),
            _relation_field_node("customer_id", ["customer_id", "first_name"]),
        ],
    )
    stmt = compile_nodes_query(orders, [fn], registry)
    return _sql(stmt, dialect_mod)


class TestDialectCompilation:
    def test_mysql_uses_json_arrayagg(self):
        sql = _relation_sql(mysql)
        assert "JSON_ARRAYAGG(JSON_OBJECT(" in sql

    def test_postgres_uses_jsonb_agg(self):
        sql = _relation_sql(postgresql)
        assert "JSONB_AGG(JSONB_BUILD_OBJECT(" in sql

    def test_default_dialect_uses_json_arrayagg(self):
        """Fallback compilation (no specific dialect) uses JSON_ARRAYAGG."""
        _, registry = _make_registry()
        orders = registry["orders"]
        fn = _field_node(
            "orders",
            [
                _field_node("order_id"),
                _relation_field_node("customer_id", ["customer_id", "first_name"]),
            ],
        )
        stmt = compile_nodes_query(orders, [fn], registry)
        # Default compilation (no specific dialect)
        sql = str(stmt)
        assert "JSON_ARRAYAGG" in sql
        assert "JSON_OBJECT" in sql

    def test_no_lateral_anywhere(self):
        """All dialects must avoid LATERAL."""
        for mod in [mysql, postgresql]:
            sql = _relation_sql(mod)
            assert "LATERAL" not in sql, f"LATERAL found in {mod.__name__}: {sql}"


class TestCompositeFKCorrelation:
    """Composite FK join predicate uses AND of all column pairs."""

    def _make_composite_registry(self) -> tuple[TableDef, TableRegistry]:
        # order_items(tenant_id, order_id) → orders(tenant_id, id)
        orders = TableDef(
            name="orders",
            database="db",
            schema="main",
            table="orders",
            columns=[
                ColumnDef(name="tenant_id", gql_type="Int", not_null=True),
                ColumnDef(name="id", gql_type="Int", not_null=True, is_pk=True),
            ],
        )
        order_items = TableDef(
            name="order_items",
            database="db",
            schema="main",
            table="order_items",
            columns=[
                ColumnDef(name="item_id", gql_type="Int", not_null=True, is_pk=True),
                ColumnDef(name="tenant_id", gql_type="Int", not_null=True),
                ColumnDef(
                    name="order_id",
                    gql_type="Int",
                    not_null=True,
                    relation=RelationDef(
                        target_model="orders",
                        target_column="id",
                        from_columns=["tenant_id", "order_id"],
                        to_columns=["tenant_id", "id"],
                    ),
                ),
            ],
        )
        return order_items, TableRegistry([orders, order_items])

    def _composite_sql(self) -> str:
        order_items, registry = self._make_composite_registry()
        fn = _field_node(
            "order_items",
            [
                _field_node("item_id"),
                _relation_field_node("order_id", ["tenant_id", "id"]),
            ],
        )
        return _sql(compile_nodes_query(order_items, [fn], registry), postgresql)

    def test_composite_predicate_contains_both_column_pairs(self):
        sql = self._composite_sql()
        assert "tenant_id" in sql
        assert "order_id" in sql

    def test_composite_predicate_uses_and(self):
        sql = self._composite_sql()
        assert " AND " in sql.upper()
