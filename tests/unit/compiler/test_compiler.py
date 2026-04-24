"""Tests for the SQL compiler (compiler.py).

Verifies that generated SQL uses the correct dialect-specific functions
when compiled against different SQLAlchemy dialects.
"""

import pytest
from sqlalchemy.dialects import mysql, postgresql

from dbt_graphql.compiler.query import compile_query
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
        stmt = compile_query(customers, [fn], registry)
        sql = _sql(stmt, postgresql)
        assert "customer_id" in sql
        assert "first_name" in sql

    def test_limit(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, limit=10)
        sql = _sql(stmt, postgresql)
        assert "LIMIT 10" in sql

    def test_offset(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, limit=10, offset=20)
        sql = _sql(stmt, postgresql)
        assert "OFFSET 20" in sql


class TestWhereFilter:
    def test_equality_filter(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, where={"customer_id": 1})
        sql = _sql(stmt, postgresql)
        assert "WHERE" in sql
        assert "1" in sql

    def test_unknown_column_raises(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        with pytest.raises(ValueError, match="nonexistent"):
            compile_query(customers, [fn], registry, where={"nonexistent": 1})

    def test_empty_where_does_not_raise(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, where={})
        sql = _sql(stmt, postgresql)
        assert "WHERE" not in sql


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
        stmt = compile_query(orders, [fn], registry)
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
        stmt = compile_query(orders, [fn], registry)
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
            compile_query(a, [fn], registry)

    def test_depth_limit_raises_when_max_depth_set(self):
        """compile_query(max_depth=N) must raise once nesting exceeds N."""
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
            compile_query(tables[0], [fn], registry, max_depth=max_depth)

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
        stmt = compile_query(orders, [fn], registry)
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
    stmt = compile_query(orders, [fn], registry)
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
        stmt = compile_query(orders, [fn], registry)
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
        return _sql(compile_query(order_items, [fn], registry), postgresql)

    def test_composite_predicate_contains_both_column_pairs(self):
        sql = self._composite_sql()
        assert "tenant_id" in sql
        assert "order_id" in sql

    def test_composite_predicate_uses_and(self):
        sql = self._composite_sql()
        assert " AND " in sql.upper()
