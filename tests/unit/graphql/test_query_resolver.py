"""Resolver-layer behavior for the unified root resolver.

Tests the simplified resolver that calls compile_query directly
and returns rows without the {T}Result carrier dict pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dbt_graphql.cache import CacheConfig
from dbt_graphql.schema.models import ColumnDef, TableDef
from dbt_graphql.graphql.resolvers import _make_root_resolver, parse_order_by


def _tdef() -> TableDef:
    return TableDef(
        name="Invoice",
        database="mydb",
        schema="main",
        table="Invoice",
        columns=[
            ColumnDef(name="InvoiceId", gql_type="Int", not_null=True, is_pk=True),
            ColumnDef(name="Total", gql_type="Float"),
        ],
    )


def _make_info(db, field_nodes=None) -> MagicMock:
    info = MagicMock()
    info.context = {
        "registry": MagicMock(),
        "db": db,
        "jwt_payload": {},
        "policy_engine": None,
        "cache_config": CacheConfig(),
    }
    info.field_nodes = field_nodes or []
    return info


@pytest.mark.asyncio
async def test_root_resolver_returns_rows_directly(monkeypatch, fresh_cache):
    """The root resolver returns a list of dicts, not a carrier dict."""
    del fresh_cache

    fake_row = {"InvoiceId": 1, "Total": 100.0}
    execute_calls = 0

    async def fake_execute(_stmt):
        nonlocal execute_calls
        execute_calls += 1
        return [fake_row]

    db = MagicMock()
    db.dialect_name = "postgresql"
    db.execute = fake_execute

    registry = MagicMock()
    registry.get.return_value = _tdef()

    info = _make_info(db)
    info.context["registry"] = registry

    resolver = _make_root_resolver("Invoice")
    result = await resolver(None, info, where=None)

    assert isinstance(result, list)
    assert result == [fake_row]
    assert execute_calls == 1


@pytest.mark.asyncio
async def test_root_resolver_calls_compile_query(monkeypatch, fresh_cache):
    """The root resolver calls compile_query with correct arguments."""
    del fresh_cache

    compile_calls = []

    def fake_compile(**kwargs):
        compile_calls.append(kwargs)
        return MagicMock(name="stmt")

    monkeypatch.setattr("dbt_graphql.graphql.resolvers.compile_query", fake_compile)

    async def fake_execute(_stmt):
        return [{"InvoiceId": 1}]

    db = MagicMock()
    db.dialect_name = "postgresql"
    db.execute = fake_execute

    registry = MagicMock()
    registry.get.return_value = _tdef()

    info = _make_info(db)
    info.context["registry"] = registry

    resolver = _make_root_resolver("Invoice")
    await resolver(None, info, where={"InvoiceId": {"_eq": 1}}, limit=10)

    assert len(compile_calls) == 1
    call = compile_calls[0]
    assert call["tdef"].name == "Invoice"
    assert call["where"] == {"InvoiceId": {"_eq": 1}}
    assert call["limit"] == 10


@pytest.mark.asyncio
async def test_root_resolver_translates_policy_error(monkeypatch, fresh_cache):
    """PolicyError is translated to GraphQLError."""
    del fresh_cache
    from graphql import GraphQLError

    from dbt_graphql.graphql.policy import ColumnAccessDenied
    from dbt_graphql.graphql.resolvers import _to_graphql_error

    exc = ColumnAccessDenied("Invoice", ["Total"])
    graphql_err = _to_graphql_error(exc)

    assert isinstance(graphql_err, GraphQLError)
    # extensions is typed as dict[str, Any] but the linter may not see that
    assert graphql_err.extensions is not None
    assert graphql_err.extensions["code"] == "FORBIDDEN_COLUMN"
    assert graphql_err.extensions["table"] == "Invoice"
    assert graphql_err.extensions["columns"] == ["Total"]


def test_parse_order_by_with_object_value():
    """parse_order_by extracts order_by from ObjectValueNode in AST."""
    from graphql.language import ObjectValueNode

    # Create a mock field node with an order_by argument
    arg = MagicMock()
    arg.name.value = "order_by"

    # Create real ObjectValueNode with fields
    field_node = MagicMock()
    field_node.name.value = "Total"
    field_node.value.value = "desc"
    field_node2 = MagicMock()
    field_node2.name.value = "InvoiceId"
    field_node2.value.value = "asc"

    obj_value = ObjectValueNode(fields=[field_node, field_node2])
    arg.value = obj_value

    field = MagicMock()
    field.arguments = [arg]

    info = MagicMock()
    info.field_nodes = [field]
    info.variable_values = {}

    result = parse_order_by(info)
    assert result == [("Total", "desc"), ("InvoiceId", "asc")]


def test_parse_order_by_empty():
    """parse_order_by returns empty list when no order_by arg present."""
    info = MagicMock()
    info.field_nodes = [MagicMock(arguments=[])]
    info.variable_values = {}

    result = parse_order_by(info)
    assert result == []


def test_parse_order_by_with_variable():
    """parse_order_by resolves variable references."""
    from graphql.language import VariableNode

    arg = MagicMock()
    arg.name.value = "order_by"

    var_node = VariableNode(name=MagicMock(value="orderByVar"))
    arg.value = var_node

    field = MagicMock()
    field.arguments = [arg]

    info = MagicMock()
    info.field_nodes = [field]
    info.variable_values = {"orderByVar": {"Total": "desc"}}

    result = parse_order_by(info)
    assert result == [("Total", "desc")]
