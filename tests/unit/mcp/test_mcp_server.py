"""Tests for MCP tool outputs (McpTools class)."""

import asyncio
from pathlib import Path

from dbt_graphql.pipeline import extract_project
from dbt_graphql.mcp.server import McpTools


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


def _make_tools() -> McpTools:
    project = extract_project(CATALOG, MANIFEST)
    return McpTools(project)


class TestListTables:
    def test_returns_table_names(self):
        tools = _make_tools()
        result = tools.list_tables()
        names = {t["name"] for t in result["tables"]}
        assert "customers" in names
        assert "orders" in names

    def test_each_table_has_column_count(self):
        tools = _make_tools()
        result = tools.list_tables()
        for t in result["tables"]:
            assert t["column_count"] > 0

    def test_has_next_steps(self):
        tools = _make_tools()
        result = tools.list_tables()
        assert len(result["_meta"]["next_steps"]) > 0


class TestDescribeTable:
    def test_returns_columns(self):
        tools = _make_tools()
        result = asyncio.run(tools.describe_table("customers"))
        col_names = {c["name"] for c in result["columns"]}
        assert "customer_id" in col_names

    def test_column_has_required_fields(self):
        tools = _make_tools()
        result = asyncio.run(tools.describe_table("orders"))
        for col in result["columns"]:
            assert "name" in col
            assert "sql_type" in col
            assert "not_null" in col
            assert "is_unique" in col
            assert "value_summary" in col

    def test_no_db_row_count_is_none(self):
        tools = _make_tools()
        result = asyncio.run(tools.describe_table("customers"))
        assert result["row_count"] is None
        assert result["sample_rows"] == []

    def test_missing_table_returns_error(self):
        tools = _make_tools()
        result = asyncio.run(tools.describe_table("no_such_table"))
        assert "error" in result

    def test_has_next_steps(self):
        tools = _make_tools()
        result = asyncio.run(tools.describe_table("customers"))
        assert len(result["_meta"]["next_steps"]) > 0


class TestFindPath:
    def test_direct_relationship_found(self):
        tools = _make_tools()
        result = tools.find_path("orders", "customers")
        assert result["found"] is True
        assert len(result["paths"]) > 0

    def test_path_step_has_required_fields(self):
        tools = _make_tools()
        result = tools.find_path("orders", "customers")
        step = result["paths"][0][0]
        assert step["from_table"] == "orders"
        assert step["to_table"] == "customers"
        assert step["from_column"]
        assert step["to_column"]

    def test_no_path_returns_not_found(self):
        tools = _make_tools()
        result = tools.find_path("customers", "stg_orders")
        assert result["found"] is False
        assert "next_steps" in result["_meta"]


class TestExploreRelationships:
    def test_orders_links_to_customers(self):
        tools = _make_tools()
        result = tools.explore_relationships("orders")
        names = {r["name"] for r in result["related_tables"]}
        assert "customers" in names

    def test_direction_is_valid(self):
        tools = _make_tools()
        result = tools.explore_relationships("orders")
        for r in result["related_tables"]:
            assert r["direction"] in ("outgoing", "incoming")
            assert r["via_column"]


class TestBuildQuery:
    def test_produces_graphql_syntax(self):
        tools = _make_tools()
        result = tools.build_query("customers", ["customer_id", "first_name"])
        assert result["table"] == "customers"
        q = result["query"]
        assert "customers" in q
        assert "customer_id" in q
        assert "first_name" in q
        assert "{" in q

    def test_fields_preserved(self):
        tools = _make_tools()
        fields = ["order_id", "status", "amount"]
        result = tools.build_query("orders", fields)
        assert result["fields"] == fields


class TestExecuteQuery:
    def test_no_db_returns_error(self):
        tools = _make_tools()
        result = asyncio.run(tools.execute_query("SELECT 1"))
        assert "error" in result


class TestMcpServerRegistration:
    def test_create_server_does_not_crash(self):
        from dbt_graphql.mcp.server import create_mcp_server

        project = extract_project(CATALOG, MANIFEST)
        mcp = create_mcp_server(project)
        assert mcp is not None
