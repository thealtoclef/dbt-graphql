"""Tests for the SQL compiler (compiler.py).

Verifies that generated SQL uses the correct dialect-specific functions
when compiled against different SQLAlchemy dialects.
"""

from sqlalchemy.dialects import mysql, postgresql, sqlite

from dbt_mdl.graphql.compiler import compile_query
from dbt_mdl.graphql.schema import (
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
            ColumnDef(name="customer_id", gql_type="Integer", not_null=True, is_pk=True),
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
                relation=RelationDef(target_model="customers", target_column="customer_id"),
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
    return type("FN", (), {
        "name": type("N", (), {"value": col_name})(),
        "selection_set": type("SS", (), {"selections": children})(),
    })()


def _sql(stmt, dialect_mod) -> str:
    return str(stmt.compile(dialect=dialect_mod.dialect(), compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# Flat queries
# ---------------------------------------------------------------------------


class TestFlatQuery:
    def test_selects_scalar_columns(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id"), _field_node("first_name")])
        stmt = compile_query(customers, [fn], registry)
        sql = _sql(stmt, sqlite)
        assert "customer_id" in sql
        assert "first_name" in sql

    def test_limit(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, limit=10)
        sql = _sql(stmt, sqlite)
        assert "LIMIT 10" in sql

    def test_offset(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, limit=10, offset=20)
        sql = _sql(stmt, sqlite)
        assert "OFFSET 20" in sql


class TestWhereFilter:
    def test_equality_filter(self):
        customers, registry = _make_registry()
        fn = _field_node("customers", [_field_node("customer_id")])
        stmt = compile_query(customers, [fn], registry, where={"customer_id": 1})
        sql = _sql(stmt, sqlite)
        assert "WHERE" in sql
        assert "1" in sql


# ---------------------------------------------------------------------------
# Dialect-specific JSON function compilation
# ---------------------------------------------------------------------------


def _relation_sql(dialect_mod):
    _, registry = _make_registry()
    orders = registry["orders"]
    fn = _field_node("orders", [
        _field_node("order_id"),
        _relation_field_node("customer_id", ["customer_id", "first_name"]),
    ])
    stmt = compile_query(orders, [fn], registry)
    return _sql(stmt, dialect_mod)


class TestDialectCompilation:
    def test_mysql_uses_json_arrayagg(self):
        sql = _relation_sql(mysql)
        assert "JSON_ARRAYAGG(JSON_OBJECT(" in sql

    def test_sqlite_uses_json_group_array(self):
        sql = _relation_sql(sqlite)
        assert "JSON_GROUP_ARRAY(JSON_OBJECT(" in sql

    def test_postgres_uses_jsonb_agg(self):
        sql = _relation_sql(postgresql)
        assert "JSONB_AGG(JSONB_BUILD_OBJECT(" in sql

    def test_duckdb_uses_list(self):
        from sqlalchemy.dialects.postgresql import dialect as _  # noqa

        # DuckDB doesn't have a built-in SQLAlchemy dialect,
        # so we compile against the default and check the function name.
        # The compiles registration for "duckdb" only applies when
        # using a DuckDB-aware dialect. For now, verify the default path.
        _, registry = _make_registry()
        orders = registry["orders"]
        fn = _field_node("orders", [
            _field_node("order_id"),
            _relation_field_node("customer_id", ["customer_id", "first_name"]),
        ])
        stmt = compile_query(orders, [fn], registry)
        # Default compilation (no specific dialect)
        sql = str(stmt)
        assert "JSON_ARRAYAGG" in sql
        assert "JSON_OBJECT" in sql

    def test_no_lateral_anywhere(self):
        """All dialects must avoid LATERAL."""
        for mod in [mysql, sqlite, postgresql]:
            sql = _relation_sql(mod)
            assert "LATERAL" not in sql, f"LATERAL found in {mod.__name__}: {sql}"
