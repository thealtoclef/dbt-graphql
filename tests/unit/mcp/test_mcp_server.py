"""Tests for MCP tool outputs (McpTools class)."""

import asyncio
from pathlib import Path

from dbt_graphql.formatter.graphql import build_registry
from dbt_graphql.graphql.app import create_graphql_subapp
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEntry,
    TablePolicy,
    Effect,
)
from dbt_graphql.pipeline import extract_project
from dbt_graphql.mcp.server import McpTools


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


class _FakeDB:
    """Stand-in for DatabaseManager: records compiled SQL and returns canned rows."""

    def __init__(self, rows=()):
        self._rows = list(rows)
        self.executed = []

    @property
    def dialect_name(self) -> str:
        return "postgresql"

    async def execute(self, stmt):
        self.executed.append(stmt)
        return list(self._rows)


def _make_tools(access_policy=None) -> McpTools:
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    bundle = create_graphql_subapp(
        registry=registry,
        db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        access_policy=access_policy,
    )
    return McpTools(
        registry,
        bundle=bundle,
        project=project,
        policy_engine=bundle.policy_engine,
    )


class TestListTables:
    def test_returns_table_names(self):
        tools = _make_tools()
        result = tools.list_tables()
        names = set(result["tables"])
        assert "customers" in names
        assert "orders" in names

    def test_returns_flat_list_of_strings(self):
        tools = _make_tools()
        result = tools.list_tables()
        assert all(isinstance(t, str) for t in result["tables"])

    def test_has_next_steps(self):
        tools = _make_tools()
        result = tools.list_tables()
        assert len(result["_meta"]["next_steps"]) > 0

    def test_filter_returns_only_matching_tables(self):
        tools = _make_tools()
        result = tools.list_tables(filter="customer")
        names = set(result["tables"])
        assert "customers" in names
        assert "orders" not in names

    def test_filter_no_match_returns_empty(self):
        tools = _make_tools()
        result = tools.list_tables(filter="xyzzy_nonexistent_term_12345")
        assert result["tables"] == []

    def test_filter_is_case_insensitive(self):
        tools = _make_tools()
        result = tools.list_tables(filter="CUSTOMER")
        assert "customers" in set(result["tables"])


class TestUsageGuide:
    """The usage guide is exposed as an MCP **resource** (not a tool).

    The static prose lives in ``mcp/usage_guide.md`` and is loaded by
    ``McpTools.usage_guide_text()``. The resource is registered on the
    FastMCP server in ``create_mcp_server`` — these tests cover the
    backing function.
    """

    def test_returns_non_empty_markdown(self):
        text = McpTools.usage_guide_text()
        assert isinstance(text, str)
        assert text.lstrip().startswith("# ")

    def test_contains_workflow_sections(self):
        text = McpTools.usage_guide_text()
        assert "list_tables" in text
        assert "describe_tables" in text
        assert "build_query" in text
        assert "run_graphql" in text

    def test_contains_policy_semantics_sections(self):
        text = McpTools.usage_guide_text()
        assert "row filters" in text.lower() or "row-level" in text.lower()
        assert "JWT" in text or "column" in text

    def test_registered_as_resource_not_tool(self):
        from dbt_graphql.mcp.server import create_mcp_server

        project = extract_project(CATALOG, MANIFEST)
        registry = build_registry(project)
        bundle = create_graphql_subapp(
            registry=registry,
            db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        )
        mcp = create_mcp_server(registry, bundle=bundle, project=project)
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        resources = asyncio.run(mcp.list_resources())
        tool_names = {getattr(t, "name", None) for t in tools}
        assert "get_usage_guide" not in tool_names
        resource_uris = [str(getattr(r, "uri", "")) for r in resources]
        assert any("usage-guide" in u for u in resource_uris)


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


class TestMcpServerRegistration:
    def test_create_server_does_not_crash(self):
        from dbt_graphql.mcp.server import create_mcp_server

        project = extract_project(CATALOG, MANIFEST)
        registry = build_registry(project)
        bundle = create_graphql_subapp(
            registry=registry,
            db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        )
        mcp = create_mcp_server(registry, bundle=bundle, project=project)
        assert mcp is not None


# ---------------------------------------------------------------------------
# Policy filtering across discovery tools
#
# These exercise the integration between McpTools and a real PolicyEngine
# built from real dbt-artifact fixtures — no hand-rolled registry, no
# mocked engine. They cover the new "MCP shares the GraphQL access
# policy" contract that landed alongside run_graphql.
# ---------------------------------------------------------------------------


def _customers_only_policy() -> AccessPolicy:
    """An access policy that authorizes ``customers`` (with ``email``
    blocked) and denies everything else.
    """
    return AccessPolicy(
        policies=[
            PolicyEntry(
                effect=Effect.ALLOW,
                name="customers-only",
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(
                            include_all=True,
                            excludes=["email"],
                        ),
                    ),
                },
            ),
        ]
    )


def _make_policy_tools() -> McpTools:
    return _make_tools(access_policy=_customers_only_policy())


class TestPolicyFiltering:
    def test_list_tables_hides_unauthorized(self):
        tools = _make_policy_tools()
        result = tools.list_tables()
        names = set(result["tables"])
        assert "customers" in names
        assert "orders" not in names

    def test_describe_tables_filters_blocked_columns(self):
        tools = _make_policy_tools()
        sdl = tools.describe_tables(["customers"])
        # "email" is excluded by policy; the SDL slice must omit it.
        assert "customer_id" in sdl
        assert "email" not in sdl

    def test_describe_tables_silently_skips_denied_table(self):
        tools = _make_policy_tools()
        sdl = tools.describe_tables(["orders"])
        # Denied table is silently skipped — same shape as nonexistent.
        assert "type orders " not in sdl

    def test_find_path_unauthorized_endpoint_returns_not_found(self):
        tools = _make_policy_tools()
        result = tools.find_path("orders", "customers")
        assert result["found"] is False
        assert "not authorized" in result["_meta"]["next_steps"][0]

    def test_explore_relationships_hides_unauthorized_neighbors(self):
        tools = _make_policy_tools()
        # customers links to orders in the fixtures; orders is denied so
        # the neighbor list must come back empty rather than leaking the name.
        result = tools.explore_relationships("customers")
        names = {r["name"] for r in result["related_tables"]}
        assert "orders" not in names

    def test_explore_relationships_denied_table_returns_empty(self):
        tools = _make_policy_tools()
        result = tools.explore_relationships("orders")
        assert result["related_tables"] == []

    def test_build_query_strips_blocked_fields(self):
        tools = _make_policy_tools()
        result = tools.build_query("customers", ["customer_id", "email"])
        assert result["fields"] == ["customer_id"]
        assert "email" not in result["query"]


# ---------------------------------------------------------------------------
# run_graphql plumbing — bundle wired, real Ariadne schema, fake DB
# ---------------------------------------------------------------------------


def _bundle_with(rows, *, access_policy: AccessPolicy | None = None):
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    return create_graphql_subapp(
        registry=registry,
        db=_FakeDB(rows),  # ty: ignore[invalid-argument-type]
        access_policy=access_policy,
    )


class TestRunGraphqlWithBundle:
    def test_executes_query_through_bundle(self):
        bundle = _bundle_with([{"customer_id": 1}, {"customer_id": 2}])
        tools = McpTools(bundle.registry, bundle=bundle)
        result = asyncio.run(tools.run_graphql("query { customers { customer_id } }"))
        assert "errors" not in result
        assert result["data"] == {"customers": [{"customer_id": 1}, {"customer_id": 2}]}

    def test_parse_error_returned_as_errors(self):
        bundle = _bundle_with([])
        tools = McpTools(bundle.registry, bundle=bundle)
        result = asyncio.run(tools.run_graphql("query { nonexistent_table }"))
        assert "errors" in result

    def test_policy_denial_propagates_as_graphql_error(self):
        access_policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="customers-only",
                    when="True",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True),
                        ),
                    },
                ),
            ]
        )
        bundle = _bundle_with([], access_policy=access_policy)
        tools = McpTools(
            bundle.registry, bundle=bundle, policy_engine=bundle.policy_engine
        )
        # ``orders`` is not in the policy → resolver raises TableAccessDenied,
        # which surfaces as a structured GraphQL error.
        result = asyncio.run(tools.run_graphql("query { orders { order_id } }"))
        assert "errors" in result
        assert any("orders" in e["message"] for e in result["errors"])


# ---------------------------------------------------------------------------
# trace_column_lineage
# ---------------------------------------------------------------------------


class TestTraceColumnLineage:
    def test_returns_upstream_and_downstream(self):
        tools = _make_tools()
        result = tools.trace_column_lineage("customers", "customer_id")
        assert result["table"] == "customers"
        assert result["column"] == "customer_id"
        # stg_customers -> customers with customer_id->customer_id
        upstream_names = [u["table"] for u in result["upstream"]]
        assert "stg_customers" in upstream_names
        assert result["downstream"] == []

    def test_unauthorized_table_returns_error(self):
        # Use the policy-engine tools where orders is denied
        tools = _make_policy_tools()
        result = tools.trace_column_lineage("orders", "order_id")
        assert "error" in result
        assert "not authorized" in result["error"]

    def test_project_none_returns_error(self):
        # McpTools initialized without project has _project = None
        registry = build_registry(extract_project(CATALOG, MANIFEST))
        bundle = create_graphql_subapp(
            registry=registry,
            db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        )
        tools = McpTools(registry, bundle=bundle)
        assert tools._project is None
        result = tools.trace_column_lineage("customers", "customer_id")
        assert "error" in result
        assert "not available" in result["error"]

    def test_policy_filtering_strips_invisible_edges(self):
        # Policy allows only 'customers' table; stg_customers is not authorized,
        # so the edge stg_customers->customers must be stripped.
        tools = _make_policy_tools()
        result = tools.trace_column_lineage("customers", "customer_id")
        # stg_customers is not visible to this policy, so upstream must be empty
        assert result["upstream"] == []


class TestTraceColumnLineageRegistration:
    def test_create_server_with_trace_column_lineage(self):
        from dbt_graphql.mcp.server import create_mcp_server

        project = extract_project(CATALOG, MANIFEST)
        registry = build_registry(project)
        bundle = create_graphql_subapp(
            registry=registry,
            db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        )
        mcp = create_mcp_server(registry, bundle=bundle, project=project)
        assert mcp is not None
