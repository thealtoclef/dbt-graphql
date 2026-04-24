"""Integration tests: access policy + compile_query + SQLAlchemy.

These tests compile real SQL (against the postgresql dialect) to prove that
policy actually restricts columns, applies masks, injects row filters, and
raises on unauthorized access — and, critically, that JWT claim values are
bound as parameters and cannot inject SQL. Nested-relation policy
enforcement is exercised alongside the root-table cases.
"""

from __future__ import annotations

import pytest
from graphql import parse
from sqlalchemy.dialects import postgresql

from dbt_graphql.api.policy import (
    AccessPolicy,
    ColumnAccessDenied,
    ColumnLevelPolicy,
    PolicyEngine,
    PolicyEntry,
    TableAccessDenied,
    TablePolicy,
)
from dbt_graphql.api.security import JWTPayload
from dbt_graphql.compiler.query import compile_query
from dbt_graphql.formatter.schema import (
    ColumnDef,
    RelationDef,
    TableDef,
    TableRegistry,
)


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


def _customers_orders_registry() -> tuple[TableDef, TableDef, TableRegistry]:
    """Two tables + FK from customers.primary_order_id → orders.order_id."""
    orders = TableDef(
        name="orders",
        database="mydb",
        schema="main",
        table="orders",
        columns=[
            ColumnDef(name="order_id", gql_type="Integer", not_null=True, is_pk=True),
            ColumnDef(name="status", gql_type="Text"),
            ColumnDef(name="internal_notes", gql_type="Text"),
            ColumnDef(name="customer_id", gql_type="Integer"),
        ],
    )
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
            ColumnDef(name="primary_order_id", gql_type="Integer"),
            ColumnDef(
                name="primary_order",
                gql_type="orders",
                relation=RelationDef(
                    target_model="orders",
                    target_column="order_id",
                ),
            ),
        ],
    )
    return customers, orders, TableRegistry([customers, orders])


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
            customers,
            fields,
            registry,
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
            customers,
            fields,
            registry,
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
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.table == "customers"
    assert exc_info.value.columns == ["email", "ssn"]
    assert exc_info.value.code == "FORBIDDEN_COLUMN"


def test_strict_excludes_raises_on_excluded_column():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
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
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.columns == ["ssn"]


def test_default_deny_at_root_raises_table_denied():
    customers, registry = _customers_registry()
    engine = _engine(
        PolicyEntry(
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
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.table == "customers"


def test_no_resolve_policy_is_unrestricted_no_op():
    """When resolve_policy=None, no enforcement — parity with old tests."""
    customers, registry = _customers_registry()
    fields = _nodes("{ customers { customer_id ssn email } }")
    sql = _sql(compile_query(customers, fields, registry, resolve_policy=None))
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
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    )
    assert "NULL AS ssn" in sql


def test_expression_mask_appears_in_sql():
    customers, registry = _customers_registry()
    engine = _engine(
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
    )
    fields = _nodes("{ customers { email } }")
    sql = _sql(
        compile_query(
            customers,
            fields,
            registry,
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
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_level="org_id = {{ jwt.claims.org_id }}",
                )
            },
        )
    )

    fields = _nodes("{ customers { customer_id } }")
    stmt = compile_query(
        customers,
        fields,
        registry,
        resolve_policy=_resolver(engine, JWTPayload({"claims": {"org_id": 42}})),
    )

    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "org_id =" in str(compiled)
    assert 42 in compiled.params.values()


def test_row_filter_injection_attempt_does_not_inject():
    customers, registry = _customers_registry()
    engine = _engine(
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
    )
    fields = _nodes("{ customers { customer_id } }")
    stmt = compile_query(
        customers,
        fields,
        registry,
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
            name="analyst",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_level="org_id = {{ jwt.claims.org_id }}",
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id } }")
    sql = _sql(
        compile_query(
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({"claims": {"org_id": 42}})),
            where={"customer_id": 1},
        )
    )
    assert "org_id" in sql
    assert "customer_id" in sql


def test_no_policy_compiles_identically_to_no_argument():
    customers, registry = _customers_registry()
    fields = _nodes("{ customers { customer_id email } }")
    baseline = _sql(compile_query(customers, fields, registry))
    with_none = _sql(compile_query(customers, fields, registry, resolve_policy=None))
    assert baseline == with_none


# ---------------------------------------------------------------------------
# Nested relations: policy must still apply
# ---------------------------------------------------------------------------


def test_nested_relation_denies_when_target_table_unlisted():
    """Querying customers { primary_order { ... } } must deny if no policy
    covers orders — otherwise nested queries are a blanket policy bypass."""
    customers, _orders, registry = _customers_orders_registry()
    engine = _engine(
        PolicyEntry(
            name="customers_only",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True)
                )
            },
        )
    )
    fields = _nodes("{ customers { customer_id primary_order { order_id } } }")
    with pytest.raises(TableAccessDenied) as exc_info:
        compile_query(
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.table == "orders"


def test_nested_relation_strict_column_rejects_unauthorized_child_column():
    customers, _orders, registry = _customers_orders_registry()
    engine = _engine(
        PolicyEntry(
            name="all",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True)
                ),
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["order_id"])
                ),
            },
        )
    )
    fields = _nodes(
        "{ customers { customer_id primary_order { order_id internal_notes } } }"
    )
    with pytest.raises(ColumnAccessDenied) as exc_info:
        compile_query(
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    assert exc_info.value.table == "orders"
    assert exc_info.value.columns == ["internal_notes"]


def test_nested_relation_mask_applied_inside_subquery():
    customers, _orders, registry = _customers_orders_registry()
    engine = _engine(
        PolicyEntry(
            name="all",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True)
                ),
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True, mask={"internal_notes": None}
                    )
                ),
            },
        )
    )
    fields = _nodes(
        "{ customers { customer_id primary_order { order_id internal_notes } } }"
    )
    sql = _sql(
        compile_query(
            customers,
            fields,
            registry,
            resolve_policy=_resolver(engine, JWTPayload({})),
        )
    )
    # The JSON payload must carry a SQL NULL for internal_notes. Crucially,
    # the raw column reference (child_1.internal_notes) must NOT appear —
    # otherwise the mask was silently skipped inside the subquery.
    assert "'internal_notes', NULL" in sql
    assert "child_1.internal_notes" not in sql
    # Unmasked column still reads its real value.
    assert "'order_id', child_1.order_id" in sql


def test_nested_relation_row_filter_applied_to_subquery():
    customers, _orders, registry = _customers_orders_registry()
    engine = _engine(
        PolicyEntry(
            name="all",
            when="True",
            tables={
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True)
                ),
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_level="customer_id = {{ jwt.claims.cust_id }}",
                ),
            },
        )
    )
    fields = _nodes("{ customers { customer_id primary_order { order_id } } }")
    stmt = compile_query(
        customers,
        fields,
        registry,
        resolve_policy=_resolver(engine, JWTPayload({"claims": {"cust_id": 7}})),
    )
    # Literal-binds rendering: the row filter must appear verbatim with
    # the bind value inlined inside the subquery.
    sql = _sql(stmt)
    assert "customer_id = 7" in sql
    # The filter must NOT be applied at the outer level — that would
    # filter customers.customer_id (wrong semantics) instead of
    # orders.customer_id.
    assert "_parent.customer_id = 7" not in sql
