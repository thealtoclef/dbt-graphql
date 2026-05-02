"""Integration tests: access policy + compile_query + SQLAlchemy.

These tests compile real SQL (against the postgresql dialect) to prove that
policy actually restricts columns, applies masks, injects row filters, and
raises on unauthorized access — and, critically, that JWT claim values are
bound as parameters and cannot inject SQL.
"""

from __future__ import annotations

import pytest
from graphql import parse
from sqlalchemy.dialects import postgresql

from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnAccessDenied,
    ColumnLevelPolicy,
    PolicyEngine,
    PolicyEntry,
    TableAccessDenied,
    TablePolicy,
    Effect,
)
from dbt_graphql.graphql.auth import JWTPayload
from dbt_graphql.compiler.query import compile_query
from dbt_graphql.schema.models import ColumnDef, TableDef, TableRegistry


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


def _nodes(query: str) -> list:
    """Parse a GraphQL query and return the top-level field nodes.

    ``compile_query`` walks real ``graphql-core`` AST nodes at runtime;
    tests build the same shape via ``graphql.parse`` instead of
    hand-rolled duck types so any AST change surfaces here too.
    """
    from graphql.language import OperationDefinitionNode

    doc = parse(query)
    op = doc.definitions[0]
    assert isinstance(op, OperationDefinitionNode)
    return list(op.selection_set.selections)


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _engine(*entries: PolicyEntry) -> PolicyEngine:
    return PolicyEngine(AccessPolicy(policies=list(entries)))


def _resolver(engine: PolicyEngine, ctx: JWTPayload):
    return lambda t: engine.evaluate(t, ctx)


# ---------------------------------------------------------------------------
# Column-level policy → generated SQL
# ---------------------------------------------------------------------------


def test_blocked_column_is_stripped_from_sql():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, excludes=["ssn"])
                )
            },
        )
    )

    fields = _nodes("{ customers { customer_id email } }")
    sql = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    )
    assert "customer_id" in sql
    assert "email" in sql
    assert "ssn" not in sql


def test_includes_whitelist_in_sql():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["customer_id", "email"])
                )
            },
        )
    )

    fields = _nodes("{ customers { customer_id email } }")
    sql = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    )
    assert "customer_id" in sql
    assert "email" in sql


# ---------------------------------------------------------------------------
# Strict mode: querying unauthorized columns raises
# ---------------------------------------------------------------------------


def test_strict_includes_raises_on_unlisted_column():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="limited",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["customer_id"])
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id email ssn } }")
    with pytest.raises(ColumnAccessDenied) as exc_info:
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.table == "customers"
    assert exc_info.value.columns == ["email", "ssn"]
    assert exc_info.value.code == "FORBIDDEN_COLUMN"


def test_strict_excludes_raises_on_excluded_column():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, excludes=["ssn"])
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id ssn } }")
    with pytest.raises(ColumnAccessDenied) as exc_info:
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.columns == ["ssn"]


def test_default_deny_at_root_raises_table_denied():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="orders_only",
            when="True",
            tables={
                "orders": TablePolicy(column_level=ColumnLevelPolicy(include_all=True))
            },
        )
    )
    fields = _nodes("{ customers { customer_id } }")
    with pytest.raises(TableAccessDenied) as exc_info:
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.table == "customers"


def test_no_resolve_policy_is_unrestricted_no_op():
    """When resolve_policy=None, no enforcement — parity with old tests."""
    customers, registry = _customers_registry()
    fields = _nodes("{ customers { customer_id ssn email } }")
    sql = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
    )
    assert "customer_id" in sql
    assert "ssn" in sql
    assert "email" in sql


# ---------------------------------------------------------------------------
# Mask → generated SQL
# ---------------------------------------------------------------------------


def test_null_mask_appears_in_sql():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, mask={"ssn": None})
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id ssn } }")
    sql = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    )
    assert "NULL AS ssn" in sql


def test_expression_mask_appears_in_sql():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
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
    )
    fields = _nodes("{ customers { email } }")
    sql = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    )
    assert "CONCAT" in sql
    assert "SPLIT_PART" in sql


# ---------------------------------------------------------------------------
# Row-level policy → bound parameters (SQL injection regression)
# ---------------------------------------------------------------------------


def test_row_filter_uses_bind_param():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_filter={"org_id": {"_eq": {"jwt": "claims.org_id"}}},
                )
            },
        )
    )

    fields = _nodes("{ customers { customer_id } }")
    stmt = compile_query(
        tdef=customers,
        field_nodes=fields,
        registry=registry,
        where=None,
        order_by=None,
        limit=None,
        distinct=None,
        resolve_policy=_resolver(engine, JWTPayload({"claims": {"org_id": 42}})),
    )

    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "org_id =" in str(compiled)
    assert 42 in compiled.params.values()


def test_row_filter_injection_attempt_does_not_inject():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_filter={"org_id": {"_eq": {"jwt": "claims.org_id"}}},
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id } }")
    stmt = compile_query(
        tdef=customers,
        field_nodes=fields,
        registry=registry,
        where=None,
        order_by=None,
        limit=None,
        distinct=None,
        resolve_policy=_resolver(
            engine,
            JWTPayload({"claims": {"org_id": "1'; DROP TABLE customers; --"}}),
        ),
    )

    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "DROP TABLE" not in str(compiled)
    assert "1'; DROP TABLE customers; --" in compiled.params.values()


def test_row_filter_combined_with_user_where():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_filter={"org_id": {"_eq": {"jwt": "claims.org_id"}}},
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id } }")
    sql = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where={"customer_id": {"_eq": 1}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({"claims": {"org_id": 42}})),
        )
    )
    assert "org_id" in sql
    assert "customer_id" in sql


def test_where_on_unauthorized_column_raises():
    """Filtering on a column the policy excludes must raise — otherwise a
    caller could probe the *value* of a hidden column via boolean side-
    channels (``where: { ssn: { _eq: '...' } }`` returning rows or none)."""
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, excludes=["ssn"])
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id } }")
    with pytest.raises(ColumnAccessDenied) as exc:
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where={"ssn": {"_eq": "123-45-6789"}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc.value.columns == ["ssn"]


def test_order_by_on_unauthorized_column_raises():
    """ORDER BY also leaks ordering signals; must enforce column policy."""
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
            effect=Effect.ALLOW,
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True, excludes=["ssn"])
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id } }")
    with pytest.raises(ColumnAccessDenied) as exc:
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=[("ssn", "asc")],
            limit=None,
            distinct=None,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc.value.columns == ["ssn"]


def test_no_policy_compiles_identically_to_no_argument():
    customers, registry = _customers_registry()
    fields = _nodes("{ customers { customer_id email } }")
    baseline = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
    )
    with_none = _sql(
        compile_query(
            tdef=customers,
            field_nodes=fields,
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
    )
    assert baseline == with_none
