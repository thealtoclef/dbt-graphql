"""Integration tests: access policy + compile_query + SQLAlchemy.

These tests compile real SQL (against the sqlite dialect) to prove that a
ResolvedPolicy actually restricts columns, applies masks, and injects a row
filter — and, critically, that JWT claim values are bound as parameters and
cannot inject SQL.
"""

from __future__ import annotations

from sqlalchemy.dialects import sqlite

from dbt_graphql.api.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEngine,
    PolicyEntry,
    TablePolicy,
)
from dbt_graphql.api.security import JWTPayload
from dbt_graphql.compiler.query import compile_query
from dbt_graphql.formatter.schema import ColumnDef, TableDef, TableRegistry


def _customers_registry() -> tuple[TableDef, TableRegistry]:
    customers = TableDef(
        name="customers",
        database="mydb",
        schema="main",
        table="customers",
        columns=[
            ColumnDef(
                name="customer_id", gql_type="Integer", not_null=True, is_pk=True
            ),
            ColumnDef(name="email", gql_type="Text"),
            ColumnDef(name="ssn", gql_type="Text"),
            ColumnDef(name="org_id", gql_type="Integer"),
            ColumnDef(name="internal_notes", gql_type="Text"),
        ],
    )
    return customers, TableRegistry([customers])


def _field_node(name, selections=None):
    class FN:
        def __init__(self, n, sels=None):
            self.name = type("N", (), {"value": n})()
            self.selection_set = None
            if sels is not None:
                self.selection_set = type("SS", (), {"selections": sels})()

    return FN(name, selections)


def _sql(stmt) -> str:
    return str(
        stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    )


def _engine(*entries: PolicyEntry) -> PolicyEngine:
    return PolicyEngine(AccessPolicy(policies=list(entries)))


# ---------------------------------------------------------------------------
# Column-level policy → generated SQL
# ---------------------------------------------------------------------------


def test_blocked_column_is_stripped_from_sql():
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, excludes=["ssn"])
                )
            },
        )
    ).evaluate("customers", JWTPayload({}))

    fn = _field_node(
        "customers",
        [_field_node("customer_id"), _field_node("email"), _field_node("ssn")],
    )
    sql = _sql(compile_query(customers, [fn], registry, resolved_policy=policy))
    assert "customer_id" in sql
    assert "email" in sql
    assert "ssn" not in sql


def test_includes_whitelist_in_sql():
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["customer_id", "email"])
                )
            },
        )
    ).evaluate("customers", JWTPayload({}))

    fn = _field_node(
        "customers",
        [
            _field_node("customer_id"),
            _field_node("email"),
            _field_node("ssn"),
            _field_node("org_id"),
        ],
    )
    sql = _sql(compile_query(customers, [fn], registry, resolved_policy=policy))
    assert "customer_id" in sql
    assert "email" in sql
    assert "ssn" not in sql
    assert "org_id" not in sql


# ---------------------------------------------------------------------------
# Mask → generated SQL
# ---------------------------------------------------------------------------


def test_null_mask_appears_in_sql():
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, mask={"ssn": None})
                )
            },
        )
    ).evaluate("customers", JWTPayload({}))

    fn = _field_node("customers", [_field_node("customer_id"), _field_node("ssn")])
    sql = _sql(compile_query(customers, [fn], registry, resolved_policy=policy))
    assert "NULL AS ssn" in sql


def test_expression_mask_appears_in_sql():
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True,
                        mask={"email": "CONCAT('***@', SPLIT_PART(email, '@', 2))"},
                    )
                )
            },
        )
    ).evaluate("customers", JWTPayload({}))

    fn = _field_node("customers", [_field_node("email")])
    sql = _sql(compile_query(customers, [fn], registry, resolved_policy=policy))
    assert "CONCAT" in sql
    assert "SPLIT_PART" in sql


# ---------------------------------------------------------------------------
# Row-level policy → bound parameters (SQL injection regression)
# ---------------------------------------------------------------------------


def test_row_filter_uses_bind_param():
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_level="org_id = {{ jwt.claims.org_id }}",
                )
            },
        )
    ).evaluate("customers", JWTPayload({"claims": {"org_id": 42}}))

    fn = _field_node("customers", [_field_node("customer_id")])
    stmt = compile_query(customers, [fn], registry, resolved_policy=policy)

    # Compile WITHOUT literal_binds to observe the parameter structure.
    compiled = stmt.compile(dialect=sqlite.dialect())
    # The compiled SQL must reference the row filter.
    assert "org_id =" in str(compiled)
    # And the bound value must survive as a real parameter (not interpolated).
    assert 42 in compiled.params.values()


def test_row_filter_injection_attempt_does_not_inject():
    """A JWT claim with SQL-breakout characters must NOT reach the rendered SQL."""
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_level="org_id = {{ jwt.claims.org_id }}",
                )
            },
        )
    ).evaluate(
        "customers",
        JWTPayload({"claims": {"org_id": "1'; DROP TABLE customers; --"}}),
    )

    fn = _field_node("customers", [_field_node("customer_id")])
    stmt = compile_query(customers, [fn], registry, resolved_policy=policy)

    # Without literal_binds: the dangerous string is a parameter value, not SQL.
    compiled = stmt.compile(dialect=sqlite.dialect())
    assert "DROP TABLE" not in str(compiled)
    # The value is carried as a parameter (safe).
    assert "1'; DROP TABLE customers; --" in compiled.params.values()


def test_row_filter_combined_with_user_where():
    customers, registry = _customers_registry()
    policy = _engine(
        PolicyEntry(
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_level="org_id = {{ jwt.claims.org_id }}",
                )
            },
        )
    ).evaluate("customers", JWTPayload({"claims": {"org_id": 42}}))

    fn = _field_node("customers", [_field_node("customer_id")])
    sql = _sql(
        compile_query(
            customers,
            [fn],
            registry,
            resolved_policy=policy,
            where={"customer_id": 1},
        )
    )
    # Both predicates must appear — policy filter AND user filter.
    assert "org_id" in sql
    assert "customer_id" in sql


def test_no_policy_compiles_identically_to_no_argument():
    """Passing resolved_policy=None must be a no-op."""
    customers, registry = _customers_registry()
    fn = _field_node("customers", [_field_node("customer_id"), _field_node("email")])
    baseline = _sql(compile_query(customers, [fn], registry))
    with_none = _sql(compile_query(customers, [fn], registry, resolved_policy=None))
    assert baseline == with_none
