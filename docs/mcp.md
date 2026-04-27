# MCP Server

The surface LLM agents actually use. Structured around **how agents plan queries**, not around the HTTP API.

**Sources:** [`src/dbt_graphql/mcp/server.py`](../src/dbt_graphql/mcp/server.py), [`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

See [architecture.md](architecture.md) for the design principle behind MCP-first positioning.

---

## Table of contents

- [1. Tools](#tools)
- [2. `SchemaDiscovery` — the engine behind the tools](#schemadiscovery--the-engine-behind-the-tools)
- [3. Transport](#transport)
- [4. Observability](#observability)
- [5. Why MCP-first matters](#why-mcp-first-matters)

---

## Tools

| Tool                                | Purpose                                                              |
|-------------------------------------|----------------------------------------------------------------------|
| `list_tables`                       | Tables the caller's policy authorizes — name, description, counts.   |
| `describe_table(name)`              | Column details for an authorized table; blocked columns are hidden.  |
| `find_path(from_table, to_table)`   | Shortest join path(s) via BFS on the relationship graph.             |
| `explore_relationships(table_name)` | Authorized tables related to the given one (outgoing / incoming).    |
| `build_query(table, fields)`        | Generate a GraphQL query string; filters fields by policy.           |
| `run_graphql(query, variables?)`    | Execute a GraphQL query through the same engine that backs `/graphql`. |

Each response includes `_meta.next_steps` — a short list guiding the agent's next tool call. This encodes the expected workflow (`list_tables` → `describe_table` → `find_path` → `build_query` → `run_graphql`) in the tool surface itself, reducing the need for system-prompt engineering on the agent side.

### Authorization model

The MCP server sits behind the same Starlette `AuthenticationMiddleware` as `/graphql`, so every request carries a verified JWT (or the empty anonymous payload). All tools honour the same `AccessPolicy` that gates GraphQL:

- Discovery tools (`list_tables`, `describe_table`, `explore_relationships`, `build_query`) **filter** their output to tables and columns the caller is authorized to see. There is no leak via "the schema lists a table I can't read."
- `run_graphql` re-executes the query through the **same Ariadne schema** with the **same per-request context** the HTTP layer would have built. Column allow-lists, masks, and row filters all apply structurally — there is no second authorization path to drift from the GraphQL one.

There is no raw-SQL tool. `run_graphql` is the only data-read tool, by design — raw SQL cannot be policy-enforced without parsing arbitrary statements, and "let the LLM execute SQL it wrote" is exactly the bypass the access policy exists to prevent.

---

## `SchemaDiscovery` — the engine behind the tools

[`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

`SchemaDiscovery` derives **structure** (tables, columns, types, FK relationships) from the same `TableRegistry` that GraphQL serves. The dbt `ProjectInfo` is layered on as **enrichment metadata only** — table/column descriptions and declared enum values that don't survive into the GraphQL SDL. This means MCP cannot expose a table or column that GraphQL won't: the registry is the single contract.

- Builds a **bidirectional adjacency list** at construction time by walking the registry: every `RelationDef` on a column becomes two edges (outgoing from the owning table, incoming on the target).
- `find_path()` runs BFS level-by-level, returning *all* shortest paths so the agent can choose between e.g. `orders → customers` and `orders → payments → customers`.
- `describe_table()` live-enriches results when a DB connection is available: `row_count`, `sample_rows` (3 rows), and per-column `value_summary` (enum, date range, or distinct values depending on SQL type and cardinality). Column enrichment respects an `EnrichmentConfig.budget` cap (default: 20 queries per call). Override via `enrichment.budget` in config or the `DBT_GRAPHQL__ENRICHMENT__BUDGET` env var.
- `build_query()` validates the candidate query against the live GraphQL schema (`graphql.validate`) before returning, so an agent never receives a string that won't parse.

---

## Transport

The MCP server uses **Streamable HTTP transport** (FastMCP's default). It mounts at `/mcp` when `serve.mcp_enabled: true` in `config.yml`, alongside the always-on GraphQL endpoint at `/graphql`. Both share one Granian process, one auth middleware, and one connection pool.

MCP input arrives as `POST /mcp`; server-to-client events stream via `GET /mcp` (SSE).

---

## Observability

fastmcp ships native OTel support built on `opentelemetry-api` (a hard dep of fastmcp, no extras required). Every tool call automatically emits a `SERVER` span with RPC semantic conventions (`rpc.system: "mcp"`, `rpc.method`, `rpc.service`) and FastMCP-specific attributes. Spans are no-ops unless a traces endpoint is configured — set `monitoring.traces.endpoint` and `monitoring.traces.protocol` in `config.yml` (see [configuration.md](configuration.md)). Distributed trace propagation via `traceparent`/`tracestate` in MCP request meta is also supported.

---

## Why MCP-first matters

An HTTP GraphQL endpoint assumes the consumer knows what to ask. An MCP surface assumes the consumer is *learning what to ask*. The latter is the agent workflow.

- GraphJin added MCP in v3 and positions it as the primary agent interface.
- Wren Engine ships an MCP server on top of its MDL.
- dbt-graphql adopts the same pattern — but grounded in dbt artifacts rather than a live DB or a separate modeling language.
