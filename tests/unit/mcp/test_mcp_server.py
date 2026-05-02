"""Tests for MCP tool outputs (McpTools class)."""

import asyncio
from pathlib import Path

import pytest

from dbt_graphql.graphql.sdl.generator import build_registry
from dbt_graphql.graphql.app import create_graphql_subapp
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEntry,
    TablePolicy,
    Effect,
)
from dbt_graphql.pipeline import extract_project
from dbt_graphql.mcp.server import McpTools, _ToolReturnedError


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


def _names(result: dict) -> set[str]:
    return {t["name"] for t in result["tables"]}


class TestListTables:
    def test_returns_table_summaries(self):
        tools = _make_tools()
        result = tools.list_tables()
        assert {"customers", "orders"} <= _names(result)

    def test_each_entry_has_name_and_description(self):
        tools = _make_tools()
        result = tools.list_tables()
        for t in result["tables"]:
            assert isinstance(t["name"], str) and t["name"]
            assert isinstance(t["description"], str)  # may be ""

    def test_has_next_steps(self):
        tools = _make_tools()
        result = tools.list_tables()
        assert len(result["_meta"]["next_steps"]) > 0


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
        assert "describe_table" in text
        assert "find_path" in text
        assert "run_graphql" in text
        assert "validate_only" in text

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
        names = _names(result)
        assert "customers" in names
        assert "orders" not in names

    def test_describe_table_filters_blocked_columns(self):
        tools = _make_policy_tools()
        sdl = tools.describe_table("customers")
        # "email" is excluded by policy; the SDL slice must omit it.
        assert "customer_id" in sdl
        assert "email" not in sdl

    def test_describe_table_silently_skips_denied_table(self):
        """Note: describe_table returns SDL filtered to allowed tables only."""
        tools = _make_policy_tools()
        sdl = tools.describe_table("orders")
        # Filtered SDL returned - orders not visible due to policy
        assert "type orders " not in sdl

    def test_find_path_unauthorized_endpoint_returns_not_found(self):
        tools = _make_policy_tools()
        result = tools.find_path("orders", "customers")
        assert result["found"] is False
        assert "not authorized" in result["_meta"]["next_steps"][0]


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
    @pytest.mark.asyncio
    async def test_executes_query_through_bundle(self, fresh_cache):
        # Real GraphQL execution touches the cashews ``cache`` singleton,
        # so this test depends on the ``fresh_cache`` fixture from
        # tests/unit/conftest.py — without it the resolver raises
        # "run cache.setup(...) before using cache" on the first hit.
        bundle = _bundle_with([{"customer_id": 1}, {"customer_id": 2}])
        tools = McpTools(bundle.registry, bundle=bundle)
        # GraphQL query uses {T}Result wrapper with nodes/pageInfo
        result = await tools.run_graphql(
            "query { customers { nodes { customer_id } } }"
        )
        assert "errors" not in result
        assert result["data"] == {
            "customers": {"nodes": [{"customer_id": 1}, {"customer_id": 2}]}
        }

    def test_parse_error_raises_typed_signal(self):
        # Direct (un-wrapped) callers receive the typed _ToolReturnedError;
        # the FastMCP-mounted wrapper catches it and returns the payload
        # dict to the agent. The typed exception is what lets the metrics
        # wrapper flip status=error without parsing the payload.
        bundle = _bundle_with([])
        tools = McpTools(bundle.registry, bundle=bundle)
        with pytest.raises(_ToolReturnedError) as ei:
            asyncio.run(tools.run_graphql("query { nonexistent_table }"))
        assert "errors" in ei.value.payload

    def test_validate_only_skips_execution_on_valid_query(self):
        bundle = _bundle_with([{"customer_id": 1}])
        tools = McpTools(bundle.registry, bundle=bundle)
        result = asyncio.run(
            tools.run_graphql(
                "query { customers { nodes { customer_id } } }", validate_only=True
            )
        )
        assert result == {"validation": "ok"}
        # Execution skipped — fake DB recorded zero queries.
        assert bundle.db.executed == []  # ty: ignore[possibly-unbound-attribute]

    def test_validate_only_raises_on_invalid_query(self):
        bundle = _bundle_with([])
        tools = McpTools(bundle.registry, bundle=bundle)
        with pytest.raises(_ToolReturnedError) as ei:
            asyncio.run(
                tools.run_graphql("query { nonexistent_table }", validate_only=True)
            )
        assert "errors" in ei.value.payload
        assert "validation" not in ei.value.payload

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
        # ``orders`` is not in the policy → resolver raises TableAccessDenied;
        # run_graphql converts to _ToolReturnedError so the metrics wrapper
        # records status=error.
        with pytest.raises(_ToolReturnedError) as ei:
            asyncio.run(tools.run_graphql("query { orders { nodes { order_id } } }"))
        assert "errors" in ei.value.payload
        assert any("orders" in e["message"] for e in ei.value.payload["errors"])


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


# ---------------------------------------------------------------------------
# Metric labeling — _ToolReturnedError must flip mcp.tool.calls.status=error
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_mcp_meter(monkeypatch):
    """Replace the cached MCP instruments with ones bound to an in-memory
    metric reader, so tests can read recorded attributes back.

    Bypasses ``metrics.set_meter_provider`` (which is one-way and refuses
    to override). Instead we inject the (counter, duration, size) tuple
    directly into the cache for the duration of the test.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from dbt_graphql.mcp import server as mcp_server

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("dbt_graphql.mcp")
    instruments = (
        meter.create_counter(name="mcp.tool.calls", unit="1"),
        meter.create_histogram(name="mcp.tool.duration", unit="ms"),
        meter.create_histogram(name="mcp.tool.result_bytes", unit="By"),
    )
    monkeypatch.setattr(
        mcp_server,
        "_get_mcp_metrics_instruments",
        lambda: instruments,
    )
    return reader


def _statuses_for(reader, metric_name: str) -> list[str]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    statuses: list[str] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != metric_name:
                    continue
                for dp in m.data.data_points:
                    statuses.append(dp.attributes.get("status"))
    return statuses


class TestInstrumentToolMetricStatus:
    """``_instrument_tool`` must label ``mcp.tool.calls`` with
    ``status=error`` whenever a tool raises ``_ToolReturnedError`` —
    detection via the typed exception, not by parsing the returned dict.
    """

    def test_typed_error_signal_records_status_error(self, isolated_mcp_meter):
        from dbt_graphql.mcp.server import _instrument_tool

        async def fails():
            raise _ToolReturnedError({"errors": [{"message": "boom"}]})

        wrapped = _instrument_tool("test_tool", fails)
        result = asyncio.run(wrapped())
        assert result == {"errors": [{"message": "boom"}]}
        assert _statuses_for(isolated_mcp_meter, "mcp.tool.calls") == ["error"]

    def test_normal_return_records_status_success(self, isolated_mcp_meter):
        from dbt_graphql.mcp.server import _instrument_tool

        async def ok():
            return {"data": "yes"}

        wrapped = _instrument_tool("test_tool", ok)
        result = asyncio.run(wrapped())
        assert result == {"data": "yes"}
        assert _statuses_for(isolated_mcp_meter, "mcp.tool.calls") == ["success"]

    def test_uncaught_exception_records_status_error_and_propagates(
        self, isolated_mcp_meter
    ):
        from dbt_graphql.mcp.server import _instrument_tool

        async def boom():
            raise RuntimeError("kaboom")

        wrapped = _instrument_tool("test_tool", boom)
        with pytest.raises(RuntimeError):
            asyncio.run(wrapped())
        assert _statuses_for(isolated_mcp_meter, "mcp.tool.calls") == ["error"]

    def test_size_histogram_carries_status_on_success_and_error(
        self, isolated_mcp_meter
    ):
        """``mcp.tool.result_bytes`` must be labeled with ``status`` on both
        the success and structured-error paths, matching the docs contract
        (all three MCP metrics share ``tool.name`` + ``status`` labels).
        """
        from dbt_graphql.mcp.server import _instrument_tool

        async def ok():
            return {"data": "yes"}

        async def fails():
            raise _ToolReturnedError({"errors": [{"message": "boom"}]})

        asyncio.run(_instrument_tool("test_tool", ok)())
        asyncio.run(_instrument_tool("test_tool", fails)())
        statuses = _statuses_for(isolated_mcp_meter, "mcp.tool.result_bytes")
        assert sorted(s for s in statuses if s is not None) == ["error", "success"]


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
