# MCP Server

The surface LLM agents actually use. Structured around **how agents plan queries**, not around the HTTP API.

**Sources:** [`src/dbt_graphql/mcp/server.py`](../src/dbt_graphql/mcp/server.py), [`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

See [architecture.md](architecture.md) for the design principle behind MCP-first positioning.

---

## Tools

| Tool                                | Purpose                                                              |
|-------------------------------------|----------------------------------------------------------------------|
| `list_tables`                       | All tables with name, description, column count, relationship count. |
| `describe_table(name)`              | Full column details, constraints, directly related tables.           |
| `find_path(from_table, to_table)`   | Shortest join path(s) via BFS on the relationship graph.             |
| `explore_relationships(table_name)` | All directly related tables with direction (outgoing / incoming).    |
| `build_query(table, fields)`        | Generate a boilerplate GraphQL query for a table and field list.     |
| `execute_query(sql)`                | Run SQL against the warehouse (requires `--config`).                 |

Each response includes `_meta.next_steps` — a short list guiding the agent's next tool call. This encodes the expected workflow (`list_tables` → `describe_table` → `find_path` → `build_query` → `execute_query`) in the tool surface itself, reducing the need for system-prompt engineering on the agent side.

---

## `SchemaDiscovery` — the engine behind the tools

[`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

- Builds a **bidirectional adjacency list** at construction time: every `RelationshipInfo` becomes two edges (outgoing from `from_model`, incoming to `to_model`).
- `find_path()` runs BFS, early-terminating when a longer path would extend. Returns *all* shortest paths, not just one — an agent benefits from seeing alternatives (`orders → customers` vs. `orders → payments → customers`).
- `describe_table()` live-enriches results when a DB connection is available: `row_count`, `sample_rows` (3 rows), and per-column `value_summary` (enum, date range, or distinct values depending on SQL type and cardinality). Column enrichment respects an `EnrichmentConfig.budget` cap (default: 20 queries per call). Override via `enrichment.budget` in config or the `DBT_GRAPHQL__ENRICHMENT__BUDGET` env var.

---

## Observability

fastmcp ships native OTel support built on `opentelemetry-api` (a hard dep of fastmcp, no extras required). Every tool call automatically emits a `SERVER` span with RPC semantic conventions (`rpc.system: "mcp"`, `rpc.method`, `rpc.service`) and FastMCP-specific attributes. Spans are no-ops unless a traces endpoint is configured — set `monitoring.traces.endpoint` and `monitoring.traces.protocol` in `config.yml` (see [configuration.md](configuration.md)). Distributed trace propagation via `traceparent`/`tracestate` in MCP request meta is also supported.

---

## Why MCP-first matters

An HTTP GraphQL endpoint assumes the consumer knows what to ask. An MCP surface assumes the consumer is *learning what to ask*. The latter is the agent workflow.

- GraphJin added MCP in v3 and positions it as the primary agent interface.
- Wren Engine ships an MCP server on top of its MDL.
- dbt-graphql adopts the same pattern — but grounded in dbt artifacts rather than a live DB or a separate modeling language.
