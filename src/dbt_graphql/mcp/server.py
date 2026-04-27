"""MCP server exposing schema discovery and query tools for LLM agents."""

from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable


from .discovery import SchemaDiscovery


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

    # Use setattr to attach __signature__ — direct assignment trips the
    # type checker because Callable doesn't formally carry the slot, but
    # FastMCP's tool registration reads it via getattr.
    setattr(wrapper, "__signature__", inspect.signature(func))

    return wrapper


class McpTools:
    """Tool functions exposed to LLM agents via MCP.

    Instantiate directly for testing; wrap with create_mcp_server for serving.
    """

    def __init__(self, project, db=None, enrichment=None) -> None:
        self._discovery = SchemaDiscovery(project, db=db, enrichment=enrichment)
        self._db = db

    def list_tables(self) -> dict[str, Any]:
        """List all available tables with summary information."""
        tables = self._discovery.list_tables()
        return {
            "tables": [
                {
                    "name": t.name,
                    "description": t.description,
                    "column_count": t.column_count,
                    "relationship_count": t.relationship_count,
                }
                for t in tables
            ],
            "_meta": {
                "next_steps": [
                    "Call describe_table(name) to get full column details for a specific table.",
                    "Call explore_relationships(table_name) to see how tables connect.",
                ]
            },
        }

    async def describe_table(self, name: str) -> dict[str, Any]:
        """Get full column details for a table, including live enrichment when a DB is configured."""
        detail = await self._discovery.describe_table(name)
        if detail is None:
            return {"error": f"Table '{name}' not found.", "_meta": {}}
        return {
            "name": detail.name,
            "description": detail.description,
            "row_count": detail.row_count,
            "sample_rows": detail.sample_rows,
            "columns": [
                {
                    "name": c.name,
                    "sql_type": c.sql_type,
                    "not_null": c.not_null,
                    "is_unique": c.is_unique,
                    "description": c.description,
                    "enum_values": c.enum_values,
                    "value_summary": c.value_summary,
                }
                for c in detail.columns
            ],
            "relationships": detail.relationships,
            "_meta": {
                "next_steps": [
                    "Call find_path(from_table, to_table) to discover join paths.",
                    "Call build_query(table, fields) to generate a GraphQL query.",
                ]
            },
        }

    def find_path(self, from_table: str, to_table: str) -> dict[str, Any]:
        """Find the shortest join path between two tables."""
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
        """Get all tables directly related to the given table."""
        related = self._discovery.explore_relationships(table_name)
        return {
            "table": table_name,
            "related_tables": [
                {"name": r.name, "via_column": r.via_column, "direction": r.direction}
                for r in related
            ],
            "_meta": {
                "next_steps": [
                    "Call find_path to discover multi-hop join paths.",
                    "Call describe_table for column details of any related table.",
                ]
            },
        }

    def build_query(self, table: str, fields: list[str]) -> dict[str, Any]:
        """Generate a GraphQL query for a table with the given fields."""
        field_str = "\n    ".join(fields)
        query = f"query {{\n  {table} {{\n    {field_str}\n  }}\n}}"
        return {
            "table": table,
            "fields": fields,
            "query": query,
            "_meta": {
                "next_steps": [
                    "Pass the query to execute_query to run it against the database."
                ]
            },
        }

    async def execute_query(self, sql: str) -> dict[str, Any]:
        """Execute a raw SQL statement against the database."""
        if self._db is None:
            return {"error": "No database connection configured.", "_meta": {}}
        rows = await self._db.execute_text(sql)
        return {
            "row_count": len(rows),
            "rows": rows,
            "_meta": {
                "next_steps": [
                    "Analyze the results or call another tool for further exploration."
                ]
            },
        }


def create_mcp_server(project, db=None, enrichment=None):
    """Build and return a fastmcp Server with all tools registered."""
    from fastmcp import FastMCP

    tools = McpTools(project, db=db, enrichment=enrichment)
    mcp = FastMCP("dbt-graphql")

    # Register tools with metrics instrumentation
    mcp.tool(name="list_tables")(_instrument_tool("list_tables", tools.list_tables))
    mcp.tool(name="describe_table")(
        _instrument_tool("describe_table", tools.describe_table)
    )
    mcp.tool(name="find_path")(_instrument_tool("find_path", tools.find_path))
    mcp.tool(name="explore_relationships")(
        _instrument_tool("explore_relationships", tools.explore_relationships)
    )
    mcp.tool(name="build_query")(_instrument_tool("build_query", tools.build_query))
    mcp.tool(name="execute_query")(
        _instrument_tool("execute_query", tools.execute_query)
    )

    return mcp


def create_mcp_http_app(project, db=None, enrichment=None):
    """Return a FastMCP Starlette sub-app using Streamable HTTP transport."""
    return create_mcp_server(project, db=db, enrichment=enrichment).http_app()
