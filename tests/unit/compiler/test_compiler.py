"""Tests for the unified compile_query function.

Verifies that compile_query handles:
- Row-only queries (no aggregates)
- Aggregate-only queries
- Mixed queries (dimensions + aggregates with GROUP BY)
- Order by on dimension columns
- Order by on aggregate fields (injects into SELECT)
- distinct + aggregates = error
- relations + aggregates = error
- WHERE with all operators
- Nested AND/OR/NOT bool expressions
- Policy enforcement (blocked columns)
"""

import pytest
from sqlalchemy.dialects import postgresql

from dbt_graphql.compiler.query import compile_query
from dbt_graphql.schema.models import ColumnDef, RelationDef, TableDef, TableRegistry


def _make_invoice_registry() -> tuple[TableDef, TableRegistry]:
    """Invoice table with mixed column types for aggregate testing."""
    invoice = TableDef(
        name="Invoice",
        database="mydb",
        schema="main",
        table="Invoice",
        columns=[
            ColumnDef(name="InvoiceId", gql_type="Int", not_null=True, is_pk=True),
            ColumnDef(name="CustomerId", gql_type="Int"),
            ColumnDef(name="BillingState", gql_type="String"),
            ColumnDef(name="Total", gql_type="Float"),
            ColumnDef(name="CreatedAt", gql_type="String"),
        ],
    )
    return invoice, TableRegistry([invoice])


def _make_customers_registry() -> tuple[TableDef, TableRegistry]:
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
            ColumnDef(name="email", gql_type="Text"),
            ColumnDef(name="status", gql_type="String"),
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
            ColumnDef(name="amount", gql_type="Float"),
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


def _sql(stmt, dialect_mod=postgresql) -> str:
    return str(
        stmt.compile(
            dialect=dialect_mod.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# ---------------------------------------------------------------------------
# Row-only queries
# ---------------------------------------------------------------------------


class TestRowOnlyQueries:
    def test_selects_scalar_columns(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice", [_field_node("InvoiceId"), _field_node("CustomerId")]
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=10,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt)
        assert "LIMIT 10" in sql

    def test_distinct(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("CustomerId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=True,
            resolve_policy=None,
        )
        sql = _sql(stmt)
        assert "DISTINCT" in sql.upper()
        assert "CustomerId" in sql


# ---------------------------------------------------------------------------
# Aggregate-only queries
# ---------------------------------------------------------------------------


class TestAggregateOnlyQueries:
    def test_count_only(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("_aggregate", [_field_node("count")])])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).lower()
        assert "count(" in sql
        assert "sum(" not in sql
        assert "group by" not in sql

    def test_sum_column(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice",
            [_field_node("_aggregate", [_field_node("sum", [_field_node("Total")])])],
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).lower()
        assert "sum(" in sql
        assert "group by" not in sql

    def test_multiple_aggregates(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice",
            [
                _field_node(
                    "_aggregate",
                    [
                        _field_node("count"),
                        _field_node("sum", [_field_node("Total")]),
                        _field_node("avg", [_field_node("Total")]),
                    ],
                ),
            ],
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).lower()
        assert "count(" in sql
        assert "sum(" in sql
        assert "avg(" in sql
        assert "group by" not in sql


# ---------------------------------------------------------------------------
# Mixed queries (dimensions + aggregates with GROUP BY)
# ---------------------------------------------------------------------------


class TestMixedQueries:
    def test_dimension_and_aggregates(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice",
            [
                _field_node("CustomerId"),
                _field_node(
                    "_aggregate",
                    [
                        _field_node("count"),
                        _field_node("sum", [_field_node("Total")]),
                    ],
                ),
            ],
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "GROUP BY" in sql
        assert "CUSTOMERID" in sql
        assert "COUNT(" in sql
        assert "SUM(" in sql

    def test_multiple_dimensions_group_by(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice",
            [
                _field_node("CustomerId"),
                _field_node("BillingState"),
                _field_node("_aggregate", [_field_node("count")]),
            ],
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "GROUP BY" in sql
        assert "CUSTOMERID" in sql
        assert "BILLINGSTATE" in sql

    def test_aggregates_only_no_group_by(self):
        """When only aggregates are selected (no dimension columns), no GROUP BY."""
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice",
            [
                _field_node(
                    "_aggregate",
                    [
                        _field_node("count"),
                        _field_node("sum", [_field_node("Total")]),
                    ],
                )
            ],
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "GROUP BY" not in sql


# ---------------------------------------------------------------------------
# Order by
# ---------------------------------------------------------------------------


class TestOrderBy:
    def test_order_by_dimension_asc(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("CustomerId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=[("CustomerId", "asc")],
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "ORDER BY" in sql
        assert "ASC" in sql

    def test_order_by_dimension_desc(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("CustomerId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=[("CustomerId", "desc")],
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "ORDER BY" in sql
        assert "DESC" in sql

    def test_order_by_invalid_direction_raises(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("CustomerId")])
        with pytest.raises(ValueError, match="Unknown order_by"):
            compile_query(
                tdef=invoice,
                field_nodes=[fn],
                registry=registry,
                where=None,
                order_by=[("CustomerId", "sideways")],
                limit=None,
                distinct=None,
                resolve_policy=None,
            )

    def test_multi_column_order(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice", [_field_node("CustomerId"), _field_node("BillingState")]
        )
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where=None,
            order_by=[("BillingState", "asc"), ("CustomerId", "desc")],
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "BILLINGSTATE" in sql
        assert "CUSTOMERID" in sql


# ---------------------------------------------------------------------------
# Error cases: distinct + aggregates, relations + aggregates
# ---------------------------------------------------------------------------


class TestMutualExclusivityErrors:
    def test_distinct_and_aggregates_raises(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node(
            "Invoice",
            [
                _field_node("CustomerId"),
                _field_node("_aggregate", [_field_node("count")]),
            ],
        )
        with pytest.raises(ValueError, match="distinct and aggregate fields"):
            compile_query(
                tdef=invoice,
                field_nodes=[fn],
                registry=registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=True,
                resolve_policy=None,
            )

    def test_relations_and_aggregates_raises(self):
        """Relations (nested selects) cannot be combined with aggregates."""
        customers, registry = _make_customers_registry()
        fn = _field_node(
            "orders",
            [
                _field_node("order_id"),
                _relation_field_node("customer_id", ["customer_id"]),
                _field_node("_aggregate", [_field_node("count")]),
            ],
        )
        with pytest.raises(ValueError, match="aggregate fields cannot be selected"):
            compile_query(
                tdef=registry["orders"],
                field_nodes=[fn],
                registry=registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=None,
            )


# ---------------------------------------------------------------------------
# WHERE operators
# ---------------------------------------------------------------------------


class TestWhereOperators:
    """Test all WHERE operators: _eq, _neq, _gt, _gte, _lt, _lte, _in, _nin,
    _is_null, _like, _nlike, _ilike, _nilike, _regex, _iregex"""

    def _where_sql(self, op, value):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={"InvoiceId": {op: value}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        return _sql(stmt).upper()

    def test_eq(self):
        sql = self._where_sql("_eq", 1)
        assert "WHERE" in sql
        assert "1" in sql

    def test_neq(self):
        sql = self._where_sql("_neq", 1)
        assert "WHERE" in sql

    def test_gt(self):
        sql = self._where_sql("_gt", 100)
        assert ">" in sql

    def test_gte(self):
        sql = self._where_sql("_gte", 100)
        assert ">=" in sql

    def test_lt(self):
        sql = self._where_sql("_lt", 100)
        assert "<" in sql

    def test_lte(self):
        sql = self._where_sql("_lte", 100)
        assert "<=" in sql

    def test_in(self):
        sql = self._where_sql("_in", [1, 2, 3])
        assert "IN" in sql

    def test_nin(self):
        sql = self._where_sql("_nin", [1, 2, 3])
        assert "NOT IN" in sql.upper()

    def test_is_null_true(self):
        sql = self._where_sql("_is_null", True)
        assert "IS NULL" in sql.upper()

    def test_is_null_false(self):
        sql = self._where_sql("_is_null", False)
        assert "IS NOT NULL" in sql.upper()

    def test_like(self):
        sql = self._where_sql("_like", "%test%")
        assert "LIKE" in sql.upper()

    def test_nlike(self):
        sql = self._where_sql("_nlike", "%test%")
        assert "NOT LIKE" in sql.upper()

    def test_ilike(self):
        sql = self._where_sql("_ilike", "%test%")
        assert "ILIKE" in sql.upper()

    def test_nilike(self):
        sql = self._where_sql("_nilike", "%test%")
        assert "NOT ILIKE" in sql.upper()

    def test_regex(self):
        sql = self._where_sql("_regex", "^test")
        assert "REGEXP" in sql.upper() or "~" in sql

    def test_iregex(self):
        """Case-insensitive regex - PostgreSQL uses (?I) flag."""
        sql = self._where_sql("_iregex", "^test")
        # PostgreSQL renders case-insensitive regex with (?I) flag
        assert "(?I)" in sql


# ---------------------------------------------------------------------------
# Nested AND/OR/NOT bool expressions
# ---------------------------------------------------------------------------


class TestBoolExpressions:
    def test_and_combinator(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={
                "_and": [
                    {"InvoiceId": {"_eq": 1}},
                    {"CustomerId": {"_eq": 100}},
                ]
            },
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "AND" in sql

    def test_or_combinator(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={
                "_or": [
                    {"InvoiceId": {"_eq": 1}},
                    {"InvoiceId": {"_eq": 2}},
                ]
            },
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "OR" in sql

    def test_not_combinator(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={"_not": {"InvoiceId": {"_eq": 1}}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "NOT" in sql or "!" in sql

    def test_nested_and_or(self):
        """_and at top level containing an _or."""
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={
                "_and": [
                    {"CustomerId": {"_eq": 1}},
                    {
                        "_or": [
                            {"InvoiceId": {"_eq": 10}},
                            {"InvoiceId": {"_eq": 20}},
                        ]
                    },
                ]
            },
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt).upper()
        assert "AND" in sql
        assert "OR" in sql

    def test_empty_where_does_not_add_clause(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=None,
        )
        sql = _sql(stmt)
        assert "WHERE" not in sql


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------


class TestPolicyEnforcement:
    def _make_resolver(self, blocked_columns=None, allowed_columns=None):
        from dbt_graphql.graphql.policy import ResolvedPolicy

        def resolve_policy(table_name):
            return ResolvedPolicy(
                allowed_columns=frozenset(allowed_columns) if allowed_columns else None,
                blocked_columns=frozenset(blocked_columns)
                if blocked_columns
                else frozenset(),
                masks={},
                row_filter_clause=None,
            )

        return resolve_policy

    def test_blocked_column_raises(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId"), _field_node("Total")])
        resolver = self._make_resolver(blocked_columns=["Total"])
        from dbt_graphql.graphql.policy import ColumnAccessDenied

        with pytest.raises(ColumnAccessDenied) as exc:
            compile_query(
                tdef=invoice,
                field_nodes=[fn],
                registry=registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=resolver,
            )
        assert exc.value.columns == ["Total"]

    def test_allowed_columns_whitelist(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId"), _field_node("Total")])
        resolver = self._make_resolver(allowed_columns=["InvoiceId"])
        from dbt_graphql.graphql.policy import ColumnAccessDenied

        with pytest.raises(ColumnAccessDenied) as exc:
            compile_query(
                tdef=invoice,
                field_nodes=[fn],
                registry=registry,
                where=None,
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=resolver,
            )
        assert "Total" in exc.value.columns

    def test_order_by_blocked_column_raises(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        resolver = self._make_resolver(blocked_columns=["Total"])
        from dbt_graphql.graphql.policy import ColumnAccessDenied

        with pytest.raises(ColumnAccessDenied) as exc:
            compile_query(
                tdef=invoice,
                field_nodes=[fn],
                registry=registry,
                where=None,
                order_by=[("Total", "asc")],
                limit=None,
                distinct=None,
                resolve_policy=resolver,
            )
        assert exc.value.columns == ["Total"]

    def test_where_blocked_column_raises(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId")])
        resolver = self._make_resolver(blocked_columns=["Total"])
        from dbt_graphql.graphql.policy import ColumnAccessDenied

        with pytest.raises(ColumnAccessDenied) as exc:
            compile_query(
                tdef=invoice,
                field_nodes=[fn],
                registry=registry,
                where={"Total": {"_eq": 100}},
                order_by=None,
                limit=None,
                distinct=None,
                resolve_policy=resolver,
            )
        assert exc.value.columns == ["Total"]

    def test_policy_where_allowed_column_succeeds(self):
        invoice, registry = _make_invoice_registry()
        fn = _field_node("Invoice", [_field_node("InvoiceId"), _field_node("Total")])
        resolver = self._make_resolver(blocked_columns=["BillingState"])
        # Should not raise
        stmt = compile_query(
            tdef=invoice,
            field_nodes=[fn],
            registry=registry,
            where={"Total": {"_eq": 100}},
            order_by=None,
            limit=None,
            distinct=None,
            resolve_policy=resolver,
        )
        sql = _sql(stmt)
        assert "Total" in sql
