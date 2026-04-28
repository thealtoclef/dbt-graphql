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
from ..formatter.sdl_view import effective_document, render_sdl
from ..graphql.app import GraphQLBundle
from ..graphql.auth import JWTPayload
from ..graphql.effective import effective_registry
from ..graphql.policy import PolicyEngine, PolicyError, ResolvedPolicy
from .discovery import SchemaDiscovery

_USAGE_GUIDE_PATH = Path(__file__).with_name("usage_guide.md")


@functools.lru_cache(maxsize=1)
def _get_mcp_metrics_instruments():
    """Build (counter, histogram) once on first call; cached for the process."""
    from opentelemetry import metrics

    meter = metrics.get_meter("dbt_graphql.mcp")
    counter = meter.create_counter(
        name="mcp.tool.calls",
        description="Total number of MCP tool calls",
        unit="1",
    )
    histogram = meter.create_histogram(
        name="mcp.tool.duration",
        description="MCP tool call duration in milliseconds",
        unit="ms",
    )
    return counter, histogram


def _instrument_tool(tool_name: str, func: Callable) -> Callable:
    """Wrap an MCP tool with metrics (counter + duration histogram).

    FastMCP inspects function signatures to generate the tool schema, so we
    preserve the original signature on the wrapper via functools.wraps +
    explicit __signature__ assignment.
    """
    import functools
    import inspect

    from ..monitoring import timed

    counter, histogram = _get_mcp_metrics_instruments()

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        async with timed(histogram, counter, {"tool.name": tool_name}):
            result = func(*args, **kwargs)
            if isinstance(result, Awaitable):
                result = await result
            return result

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

    The ``policy_engine`` and ``bundle`` constructor args are optional to
    keep the class testable in isolation. When both are absent the tools
    behave as unrestricted schema discovery (dev / unit-test mode).
    """

    def __init__(
        self,
        registry: TableRegistry,
        *,
        bundle: GraphQLBundle | None = None,
        project=None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._project = project
        self._discovery = SchemaDiscovery(registry, project=project)
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

    def _column_visible(
        self, resolved: ResolvedPolicy | None, column_name: str
    ) -> bool:
        # ``resolved is None`` means no policy engine configured (dev mode):
        # show everything. Otherwise delegate to the single source of truth
        # on ResolvedPolicy so MCP and compile_query agree on visibility.
        return resolved is None or resolved.is_column_allowed(column_name)

    # ---- tools ----

    def list_tables(self, filter: str | None = None) -> dict[str, Any]:
        """List tables the caller's access policy authorizes.

        Args:
            filter: If provided, only tables whose name or description contains
                this substring (case-insensitive) are returned. Filter is applied
                after visibility checks — tables the caller cannot see are never
                returned regardless of whether they match the filter.
        """
        ctx = _current_jwt()
        tables = self._discovery.list_tables()
        visible = [t for t in tables if self._is_visible(t.name, ctx)]
        if filter is not None:
            f = filter.lower()
            visible = [
                t
                for t in visible
                if f in t.name.lower() or (t.description and f in t.description.lower())
            ]
        return {
            "tables": [
                {
                    "name": t.name,
                    "description": t.description,
                    "column_count": t.column_count,
                    "relationship_count": t.relationship_count,
                }
                for t in visible
            ],
            "_meta": {
                "next_steps": [
                    "Call describe_table(name) to get full column details for a specific table.",
                    "Call explore_relationships(table_name) to see how tables connect.",
                ]
            },
        }

    def describe_table(self, name: str) -> dict[str, Any]:
        """Get column details for a table, filtered by the caller's policy."""
        ctx = _current_jwt()
        try:
            resolved = self._resolve(name, ctx)
        except PolicyError as exc:
            return {"error": str(exc), "_meta": {}}
        detail = self._discovery.describe_table(name)
        if detail is None:
            return {"error": f"Table '{name}' not found.", "_meta": {}}
        return {
            "name": detail.name,
            "description": detail.description,
            "columns": [
                {
                    "name": c.name,
                    "sql_type": c.sql_type,
                    "not_null": c.not_null,
                    "is_unique": c.is_unique,
                    "description": c.description,
                    "enum_values": c.enum_values,
                }
                for c in detail.columns
                if self._column_visible(resolved, c.name)
            ],
            "relationships": detail.relationships,
            "_meta": {
                "next_steps": [
                    "Call find_path(from_table, to_table) to discover join paths.",
                    "Call build_query(table, fields) to generate a GraphQL query.",
                ]
            },
        }

    def describe_tables(self, names: list[str]) -> str:
        """Return the effective ``db.graphql`` SDL slice for ``names``.

        The output is plain SDL — type definitions with full custom
        directives (``@table``, ``@column``, ``@relation``, ``@masked``,
        ``@filtered``).

        Empty input is rejected. Names not in the caller's effective
        registry are rejected with a single error shape so the response
        does not reveal whether the table exists at all.
        """
        if not names:
            raise ValueError(
                "describe_tables requires at least one table name. "
                "Call list_tables first to choose candidates."
            )
        if self._bundle is None:
            raise RuntimeError("describe_tables requires a configured GraphQL bundle.")

        ctx = _current_jwt()
        eff = effective_registry(self._bundle.registry, ctx, self._policy_engine)
        visible = {t.name for t in eff}
        unknown = [n for n in names if n not in visible]
        if unknown:
            raise ValueError(
                f"unknown or unauthorized table(s): {sorted(set(unknown))}"
            )

        doc = effective_document(self._bundle.source_doc, eff, restrict_to=set(names))
        return render_sdl(doc)

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
                        "Try explore_relationships to see what each table connects to."
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
                    "Use build_query to construct a query using these joins."
                ]
            },
        }

    def explore_relationships(self, table_name: str) -> dict[str, Any]:
        """Return tables related to ``table_name`` that the caller can see."""
        ctx = _current_jwt()
        if not self._is_visible(table_name, ctx):
            return {
                "table": table_name,
                "related_tables": [],
                "_meta": {
                    "next_steps": ["This table is not authorized for this caller."]
                },
            }
        related = self._discovery.explore_relationships(table_name)
        return {
            "table": table_name,
            "related_tables": [
                {"name": r.name, "via_column": r.via_column, "direction": r.direction}
                for r in related
                if self._is_visible(r.name, ctx)
            ],
            "_meta": {
                "next_steps": [
                    "Call find_path to discover multi-hop join paths.",
                    "Call describe_table for column details of any related table.",
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
                    "Use build_query on the upstream/downstream tables to "
                    "construct queries against them."
                ]
            },
        }

    def build_query(self, table: str, fields: list[str]) -> dict[str, Any]:
        """Generate a GraphQL query string for ``table``.

        Filters fields by policy when configured, then — when a bundle is
        available — validates the candidate against the live GraphQL
        schema so the agent never receives a string that won't parse.
        """
        ctx = _current_jwt()
        try:
            resolved = self._resolve(table, ctx)
        except PolicyError as exc:
            return {"error": str(exc), "_meta": {}}
        visible_fields = [f for f in fields if self._column_visible(resolved, f)]
        field_str = "\n    ".join(visible_fields)
        query = f"query {{\n  {table} {{\n    {field_str}\n  }}\n}}"
        if self._bundle is not None:
            try:
                doc = parse(query)
                errors = validate(self._bundle.schema, doc)
            except Exception as exc:
                return {"error": f"generated query is invalid: {exc}", "_meta": {}}
            if errors:
                return {
                    "error": "generated query failed schema validation: "
                    + "; ".join(e.message for e in errors),
                    "_meta": {},
                }
        return {
            "table": table,
            "fields": visible_fields,
            "query": query,
            "_meta": {
                "next_steps": [
                    "Pass the query to run_graphql to execute it through the policy-enforced GraphQL engine."
                ]
            },
        }

    async def run_graphql(
        self, query: str, variables: dict | None = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query through the same engine as ``/graphql``.

        The query runs against the same executable schema and per-request
        context the HTTP layer uses, so column allow-lists, masks, and
        row filters all apply. The same ``validation_rules`` (depth, field
        count, list-pagination cap) are applied here that the HTTP path
        uses — wired through ``GraphQLBundle.validation_rules`` so the two
        transports cannot drift. Returns ``{data, errors}``.
        """
        if self._bundle is None:
            return {
                "errors": [
                    {"message": "GraphQL bundle not configured for this MCP server."}
                ]
            }

        from graphql import GraphQLError

        try:
            document = parse(query)
        except GraphQLError as exc:
            return {
                "errors": [
                    {
                        "message": str(exc),
                        "extensions": exc.extensions or {},
                    }
                ]
            }

        # Combine spec rules + our custom guards — same composition Ariadne
        # does for the HTTP path (see ariadne.graphql.validate_query).
        rules = tuple(specified_rules) + tuple(self._bundle.validation_rules)
        validation_errors = validate(self._bundle.schema, document, rules)
        if validation_errors:
            return {
                "errors": [
                    {
                        "message": e.message,
                        "path": list(e.path) if e.path else None,
                        "extensions": e.extensions or {},
                    }
                    for e in validation_errors
                ]
            }

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
        return _format_execution_result(execution_result)

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
    bundle: GraphQLBundle | None = None,
    project=None,
    policy_engine: PolicyEngine | None = None,
):
    """Build and return a fastmcp Server with all tools registered.

    ``registry`` is the structural source (same one GraphQL serves);
    ``project`` is optional and contributes only dbt enrichment metadata
    (table/column descriptions, declared enums).
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
    mcp.tool(name="describe_table")(
        _instrument_tool("describe_table", tools.describe_table)
    )
    mcp.tool(name="describe_tables")(
        _instrument_tool("describe_tables", tools.describe_tables)
    )
    mcp.tool(name="find_path")(_instrument_tool("find_path", tools.find_path))
    mcp.tool(name="explore_relationships")(
        _instrument_tool("explore_relationships", tools.explore_relationships)
    )
    mcp.tool(name="trace_column_lineage")(
        _instrument_tool("trace_column_lineage", tools.trace_column_lineage)
    )
    mcp.tool(name="build_query")(_instrument_tool("build_query", tools.build_query))
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
        return server.http_app()

    return _factory
