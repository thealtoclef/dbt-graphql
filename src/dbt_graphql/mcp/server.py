"""MCP server exposing schema discovery and query tools for LLM agents.

Tools share the same JWT verification middleware as the GraphQL endpoint
(both are mounted under one Starlette app with one ``AuthenticationMiddleware``)
and the same ``AccessPolicy``: discovery tools filter the schema view to
what the caller's policy authorizes, and ``run_graphql`` re-executes the
query through the GraphQL engine so column allow-lists, masks, and row
filters all apply uniformly. Raw SQL execution from MCP is not supported
— ``run_graphql`` is the only way to read data from the warehouse.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from graphql import ExecutionResult, execute, parse, validate
from graphql.validation import specified_rules

from ..formatter.schema import TableRegistry
from ..graphql.app import GraphQLBundle
from ..graphql.auth import JWTPayload
from ..graphql.policy import PolicyEngine, PolicyError
from .discovery import SchemaDiscovery

_USAGE_GUIDE_PATH = Path(__file__).with_name("usage_guide.md")


@functools.lru_cache(maxsize=1)
def _get_mcp_metrics_instruments():
    """Build (counter, duration histogram, size histogram) once; cached for the process."""
    from opentelemetry import metrics

    meter = metrics.get_meter("dbt_graphql.mcp")
    counter = meter.create_counter(
        name="mcp.tool.calls",
        description="Total number of MCP tool calls",
        unit="1",
    )
    duration = meter.create_histogram(
        name="mcp.tool.duration",
        description="MCP tool call duration in milliseconds",
        unit="ms",
    )
    size = meter.create_histogram(
        name="mcp.tool.result_bytes",
        description="MCP tool call result payload size in bytes",
        unit="By",
    )
    return counter, duration, size


def _result_bytes(result: Any) -> int:
    """Best-effort size of the agent-facing payload.

    Strings are measured as UTF-8 bytes. Other JSON-shaped results are
    serialised with ``default=str`` to handle Decimal/datetime the same
    way ``run_graphql`` does. Anything that fails to serialise reports 0
    rather than failing the call — observability must not break tools.
    """
    if isinstance(result, str):
        return len(result.encode("utf-8"))
    try:
        return len(json.dumps(result, default=str).encode("utf-8"))
    except Exception:
        return 0


class _ToolReturnedError(Exception):
    """Typed signal that a tool call must record ``status=error`` while still
    returning a structured payload to the agent.

    Tools raise this when their typed execution path determines failure
    (e.g. graphql-core's ``ExecutionResult.errors`` is non-empty). The
    metrics wrapper catches it so ``timed`` flips ``status=error`` via
    its normal exception path, then converts the carried ``payload``
    back into the agent-facing dict — so FastMCP serialises a normal
    tool return rather than a protocol-level error.

    Detection of "did this call error?" is structural, not derived from
    inspecting keys in the returned dict. The tool itself decides, based
    on whatever typed signal its domain provides.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload


def _instrument_tool(tool_name: str, func: Callable) -> Callable:
    """Wrap an MCP tool with metrics (counter + duration + result-size histograms).

    FastMCP inspects function signatures to generate the tool schema, so we
    preserve the original signature on the wrapper via functools.wraps +
    explicit __signature__ assignment.

    Tools that need to flag a call as ``status=error`` (e.g. ``run_graphql``
    on parse/validation/execution errors) raise ``_ToolReturnedError`` with
    the agent-facing payload. ``timed`` records ``status=error`` via its
    exception path; the wrapper then unwraps the payload and returns it
    normally so FastMCP serialises the structured error response rather
    than an MCP protocol error.
    """
    import functools
    import inspect

    from ..monitoring import timed

    counter, duration, size = _get_mcp_metrics_instruments()

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # ``timed`` mutates its own copy of base_attrs (status=success|error)
        # via the duration histogram + counter, but does not propagate that
        # back to the outer dict — so the size histogram needs its own
        # explicit ``status`` label to stay consistent across all three
        # MCP metrics.
        base = {"tool.name": tool_name}
        try:
            async with timed(duration, counter, base):
                result = func(*args, **kwargs)
                if isinstance(result, Awaitable):
                    result = await result
                size.record(_result_bytes(result), {**base, "status": "success"})
                return result
        except _ToolReturnedError as exc:
            size.record(_result_bytes(exc.payload), {**base, "status": "error"})
            return exc.payload

    setattr(wrapper, "__signature__", inspect.signature(func))

    return wrapper


# ---------------------------------------------------------------------------
# Per-request JWT extraction
# ---------------------------------------------------------------------------


def _current_jwt() -> JWTPayload:
    """Read the JWT payload from the active HTTP request.

    The Starlette ``AuthenticationMiddleware`` runs upstream of the mounted
    MCP app, so ``request.user.payload`` is always populated (anonymous
    requests carry an empty ``JWTPayload``). When called outside an HTTP
    context (unit tests, background tasks), returns an empty payload —
    the caller is responsible for whatever fallback semantics it wants.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        req = get_http_request()
    except Exception:
        return JWTPayload({})
    user = getattr(req, "user", None)
    payload = getattr(user, "payload", None)
    if isinstance(payload, JWTPayload):
        return payload
    return JWTPayload({})


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class McpTools:
    """Tool functions exposed to LLM agents via MCP.

    All tools honour the same ``AccessPolicy`` that gates the GraphQL
    endpoint: schema-discovery tools filter their output to tables and
    columns the caller is authorized to see, and ``run_graphql`` runs
    queries through the same Ariadne schema with the same per-request
    context — so masks, blocked columns, and row filters apply.

    ``bundle`` is required: ``list_tables`` / ``describe_tables`` route
    through its executable schema, and ``run_graphql`` executes against
    it. ``policy_engine`` is optional; when ``None`` the tools behave as
    unrestricted schema discovery (dev / unit-test mode), matching the
    ``access_policy=None`` path in :func:`create_graphql_subapp`.
    """

    def __init__(
        self,
        registry: TableRegistry,
        *,
        bundle: GraphQLBundle,
        project=None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._project = project
        self._discovery = SchemaDiscovery(registry)
        self._bundle = bundle
        self._policy_engine = policy_engine

    # ---- helpers ----

    def _resolve(self, table_name: str, ctx: JWTPayload):
        """Return the merged ``ResolvedPolicy`` for ``table_name`` or
        ``None`` if no policy engine is configured. Raises
        ``PolicyError`` (denies) propagate to the caller — discovery
        tools translate them into "table not visible" filtering.
        """
        if self._policy_engine is None:
            return None
        return self._policy_engine.evaluate(table_name, ctx)

    def _is_visible(self, table_name: str, ctx: JWTPayload) -> bool:
        try:
            self._resolve(table_name, ctx)
            return True
        except PolicyError:
            return False

    # ---- tools ----

    def list_tables(self) -> dict[str, Any]:
        """List tables the caller's access policy authorizes.

        Each entry carries ``name`` and ``description`` — the index-page
        projection an agent uses to triage candidates before drilling in
        via ``describe_tables``. Structural detail (columns, relations) is
        intentionally omitted; it belongs to ``describe_tables(names)``.

        Visibility is enforced upstream by the GraphQL ``_tables`` resolver —
        denied tables are never returned. The agent filters client-side
        from the returned ``{name, description}`` list.
        """
        result = self._exec_graphql("{ _tables { name description } }")
        tables: list[dict[str, Any]] = list(result.get("_tables") or [])
        return {
            "tables": tables,
            "_meta": {
                "next_steps": [
                    "Call describe_tables(names) to get the SDL slice for one or more tables.",
                    "Call find_path(from, to) to discover multi-hop join paths between tables.",
                ]
            },
        }

    def describe_tables(self, names: list[str]) -> str:
        """Return the effective ``db.graphql`` SDL slice for ``names``.

        The output is plain SDL — type definitions with full custom
        directives (``@table``, ``@column``, ``@relation``, ``@masked``,
        ``@filtered``). Names the caller cannot see (denied by policy or
        nonexistent) are silently skipped — the caller cannot probe for
        existence by inspecting errors.
        """
        result = self._exec_graphql(
            "query Q($t: [String!]) { _sdl(tables: $t) }",
            variables={"t": list(names)},
        )
        return result["_sdl"]

    def _exec_graphql(
        self, query: str, *, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an internal GraphQL query against the bundle's executable schema.

        Used by discovery tools that route through ``_sdl`` / ``_tables``
        so MCP and HTTP share a single policy-pruning code path.
        """
        ctx = _current_jwt()
        context_value = self._bundle.build_context(ctx)
        result = execute(
            self._bundle.schema,
            parse(query),
            context_value=context_value,
            variable_values=variables,
        )
        if not isinstance(result, ExecutionResult):
            raise RuntimeError(
                "internal GraphQL execution returned a coroutine; "
                "_sdl / _tables resolvers must be synchronous."
            )
        if result.errors:
            raise RuntimeError(
                "internal GraphQL execution failed: "
                + "; ".join(str(e) for e in result.errors)
            )
        return result.data or {}

    def find_path(self, from_table: str, to_table: str) -> dict[str, Any]:
        """Find the shortest join path(s) between two visible tables."""
        ctx = _current_jwt()
        if not self._is_visible(from_table, ctx) or not self._is_visible(to_table, ctx):
            return {
                "found": False,
                "from_table": from_table,
                "to_table": to_table,
                "_meta": {
                    "next_steps": [
                        "One or both tables are not authorized for this caller."
                    ]
                },
            }
        paths = self._discovery.find_path(from_table, to_table)
        if not paths:
            return {
                "found": False,
                "from_table": from_table,
                "to_table": to_table,
                "_meta": {
                    "next_steps": [
                        "Call describe_tables on each endpoint to see which @relation directives "
                        "they expose and pick a different starting point."
                    ]
                },
            }
        return {
            "found": True,
            "from_table": from_table,
            "to_table": to_table,
            "paths": [
                [
                    {
                        "from_table": s.from_table,
                        "from_column": s.from_column,
                        "to_table": s.to_table,
                        "to_column": s.to_column,
                    }
                    for s in p.steps
                ]
                for p in paths
            ],
            "_meta": {
                "next_steps": [
                    "Compose a GraphQL query that traverses these joins via @relation fields, "
                    "then pass it to run_graphql (optionally with validate_only=true first)."
                ]
            },
        }

    def trace_column_lineage(self, table: str, column: str) -> dict[str, Any]:
        """Return upstream sources and downstream consumers for a column.

        Each lineage entry includes the dbt-derived ``lineage_type`` (one of
        ``pass_through``, ``rename``, ``transformation``) so the agent can
        reason about whether the value is preserved verbatim or computed.
        Edges to tables the caller is not authorized to see are stripped.
        """
        if self._project is None:
            return {"error": "column lineage not available", "_meta": {}}

        ctx = _current_jwt()
        if not self._is_visible(table, ctx):
            return {"error": f"Table '{table}' is not authorized", "_meta": {}}

        upstream: list[dict[str, Any]] = []
        downstream: list[dict[str, Any]] = []
        for edge in self._project.column_lineage:
            if edge.target == table and self._is_visible(edge.source, ctx):
                cols = [
                    {
                        "source_column": c.source_column,
                        "lineage_type": str(c.lineage_type),
                    }
                    for c in edge.columns
                    if c.target_column == column
                ]
                if cols:
                    upstream.append({"table": edge.source, "columns": cols})
            if edge.source == table and self._is_visible(edge.target, ctx):
                cols = [
                    {
                        "target_column": c.target_column,
                        "lineage_type": str(c.lineage_type),
                    }
                    for c in edge.columns
                    if c.source_column == column
                ]
                if cols:
                    downstream.append({"table": edge.target, "columns": cols})

        return {
            "table": table,
            "column": column,
            "upstream": upstream,
            "downstream": downstream,
            "_meta": {
                "next_steps": [
                    "Call describe_tables on upstream or downstream tables for column details, "
                    "then write a query and pass it to run_graphql."
                ]
            },
        }

    async def run_graphql(
        self,
        query: str,
        variables: dict | None = None,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """Execute a GraphQL query through the same engine as ``/graphql``.

        The query runs against the same executable schema and per-request
        context the HTTP layer uses, so column allow-lists, masks, and
        row filters all apply. The same ``validation_rules`` (depth, field
        count, list-pagination cap) are applied here that the HTTP path
        uses — wired through ``GraphQLBundle.validation_rules`` so the two
        transports cannot drift. Returns ``{data, errors}``.

        Args:
            query: The GraphQL operation source.
            variables: Optional variable bindings for the operation.
            validate_only: When ``True``, parse and validate the query
                against the live schema (including custom query-guard
                rules) but do not execute. Returns ``{validation: "ok"}``
                on success, ``{errors: [...]}`` on failure. Useful for
                agents to verify a candidate query before committing to
                execution.
        """
        from graphql import GraphQLError

        try:
            document = parse(query)
        except GraphQLError as exc:
            raise _ToolReturnedError(
                {
                    "errors": [
                        {
                            "message": str(exc),
                            "extensions": exc.extensions or {},
                        }
                    ]
                }
            )

        # Combine spec rules + our custom guards — same composition Ariadne
        # does for the HTTP path (see ariadne.graphql.validate_query).
        rules = tuple(specified_rules) + tuple(self._bundle.validation_rules)
        validation_errors = validate(self._bundle.schema, document, rules)
        if validation_errors:
            raise _ToolReturnedError(
                {
                    "errors": [
                        {
                            "message": e.message,
                            "path": list(e.path) if e.path else None,
                            "extensions": e.extensions or {},
                        }
                        for e in validation_errors
                    ]
                }
            )

        if validate_only:
            return {"validation": "ok"}

        ctx = _current_jwt()
        context_value = self._bundle.build_context(ctx)
        result = execute(
            self._bundle.schema,
            document,
            context_value=context_value,
            variable_values=variables,
        )
        if isinstance(result, ExecutionResult):
            execution_result = result
        else:
            execution_result = await result
        formatted = _format_execution_result(execution_result)
        # ``execution_result.errors`` is the typed structural source of truth
        # — graphql-core's ExecutionResult attribute, not a parsed dict key.
        # Any execution error (including partial-success with data) flips
        # status=error at the metrics layer.
        if execution_result.errors:
            raise _ToolReturnedError(formatted)
        return formatted

    @staticmethod
    def usage_guide_text() -> str:
        """Return the static usage-guide markdown.

        Loaded from ``mcp/usage_guide.md`` so the prose lives next to the
        rest of the docs and isn't embedded in source. Exposed via MCP as a
        resource (not a tool) — clients can attach it to context without
        the LLM spending a tool call to fetch it.
        """
        return _USAGE_GUIDE_PATH.read_text()


def _format_execution_result(result: ExecutionResult) -> dict[str, Any]:
    """Project a graphql-core ExecutionResult into the agent-facing dict.

    GraphQL data values may include non-JSON-natives from resolvers
    (Decimal, datetime). Round-tripping through json.dumps with the default
    ``str`` fallback gives the agent a stable JSON view.
    """
    out: dict[str, Any] = {}
    if result.data is not None:
        out["data"] = json.loads(json.dumps(result.data, default=str))
    if result.errors:
        out["errors"] = [
            {
                "message": str(e),
                "path": list(e.path) if e.path else None,
                "extensions": e.extensions or {},
            }
            for e in result.errors
        ]
    return out


def create_mcp_server(
    registry: TableRegistry,
    *,
    bundle: GraphQLBundle,
    project=None,
    policy_engine: PolicyEngine | None = None,
):
    """Build and return a fastmcp Server with all tools registered.

    ``registry`` is the structural source (same one GraphQL serves);
    ``bundle`` is the executable GraphQL schema MCP routes discovery and
    query execution through; ``project`` is optional and contributes
    only dbt enrichment metadata (column lineage, enums) consumed by
    ``trace_column_lineage``.
    """
    from fastmcp import FastMCP

    tools = McpTools(
        registry,
        bundle=bundle,
        project=project,
        policy_engine=policy_engine,
    )
    mcp = FastMCP("dbt-graphql")

    mcp.tool(name="list_tables")(_instrument_tool("list_tables", tools.list_tables))
    mcp.tool(name="describe_tables")(
        _instrument_tool("describe_tables", tools.describe_tables)
    )
    mcp.tool(name="find_path")(_instrument_tool("find_path", tools.find_path))
    mcp.tool(name="trace_column_lineage")(
        _instrument_tool("trace_column_lineage", tools.trace_column_lineage)
    )
    mcp.tool(name="run_graphql")(_instrument_tool("run_graphql", tools.run_graphql))

    # Static usage guide as an MCP resource (not a tool). Resources can be
    # streamed into agent context by the client without burning a tool call.
    @mcp.resource(
        uri="dbt-graphql://usage-guide",
        name="Usage Guide",
        description=(
            "Workflow guide for the dbt-graphql MCP tools — recommended call "
            "order, query-guard limits, and policy semantics."
        ),
        mime_type="text/markdown",
    )
    def _usage_guide() -> str:
        return McpTools.usage_guide_text()

    return mcp


def build_mcp_factory(project):
    """Return a factory that builds the MCP HTTP sub-app from a GraphQL bundle.

    The serve layer calls this once with the GraphQL bundle, so the MCP
    server reuses the same registry (structure), executable schema,
    per-request context-builder, DB pool, and access policy without
    re-deriving any of them. The ``project`` is retained as the source
    of dbt enrichment metadata (descriptions, enums) only.
    """

    def _factory(bundle: GraphQLBundle) -> Any:
        server = create_mcp_server(
            bundle.registry,
            bundle=bundle,
            project=project,
            policy_engine=bundle.policy_engine,
        )
        return server.http_app(path="/")

    return _factory
