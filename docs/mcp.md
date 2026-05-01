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
| `list_tables()`                     | Index-page summary of tables the caller's policy authorizes — `name` and `description` per entry, no structural detail. Backed by GraphQL `_tables`. The agent filters client-side from the returned list. |
| `describe_table(table)`             | Effective `db.graphql` SDL slice for a single table — full custom directives (`@table`, `@column`, `@id`, `@relation`, `@lineage`, `@masked`, `@filtered`). Call multiple times for multiple tables (GraphJin pattern). Names the caller cannot see (denied or nonexistent) are silently skipped, so existence cannot be probed. Backed by GraphQL `_sdl(tables: ...)`. The `@relation` directives are the agent's primary source for 1-hop adjacency; reach for `find_path` only when SDL alone can't answer. |
| `find_path(from_table, to_table)`   | Shortest join path(s) via BFS on the relationship graph. The one schema-discovery tool that has no GraphQL/SDL equivalent. |
| `trace_column_lineage(table, column)` | Upstream sources and downstream consumers for a column, derived from dbt's column-level lineage. Upstream edges are also surfaced inline in SDL via `@lineage`; this tool additionally returns *downstream* consumers, which SDL alone doesn't. Edges to unauthorized tables are stripped. |
| `run_graphql(query, variables?, validate_only?)` | Execute a GraphQL query through the same engine that backs `/graphql`. Subject to the same query guards (depth, field count, list-pagination cap) as the HTTP endpoint. With `validate_only=true`, parses and validates without executing — returns `{validation: "ok"}` or `{errors: [...]}`. |

### Resources

| URI | Purpose |
|---|---|
| `dbt-graphql://usage-guide` | Markdown workflow guide for LLM agents — recommended call order, query-guard limits, `where`-arg semantics, and policy invariants. Exposed as an MCP **resource** (not a tool) so clients can stream it into agent context without burning a tool call. |

Each response includes `_meta.next_steps` — a short list guiding the agent's next tool call. This encodes the expected workflow (`list_tables` → `describe_table` → optionally `find_path` / `trace_column_lineage` → `run_graphql`) in the tool surface itself, reducing the need for system-prompt engineering on the agent side.

### Authorization model

The MCP server sits behind the same Starlette `AuthenticationMiddleware` as `/graphql`, so every request carries a verified JWT (or the empty anonymous payload). All tools honour the same `AccessPolicy` that gates GraphQL:

- Discovery tools (`list_tables`, `describe_table`, `find_path`, `trace_column_lineage`) **filter** their output to tables and columns the caller is authorized to see. `list_tables` and `describe_table` route through the GraphQL executable schema (`_tables` / `_sdl(tables: ...)`) so MCP and HTTP share a single policy-pruning code path with no risk of drift. `describe_table` silently skips unknown / policy-denied names so the caller cannot probe for existence.
- `run_graphql` re-executes the query through the **same Ariadne schema** with the **same per-request context** the HTTP layer would have built. Column allow-lists, masks, and row filters all apply structurally — there is no second authorization path to drift from the GraphQL one.

There is no raw-SQL tool. `run_graphql` is the only data-read tool, by design — raw SQL cannot be policy-enforced without parsing arbitrary statements, and "let the LLM execute SQL it wrote" is exactly the bypass the access policy exists to prevent.

---

## `SchemaDiscovery` — the engine behind the tools

[`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

`SchemaDiscovery` covers `find_path` — the one tool that has no GraphQL equivalent. Adjacency is derived from the same `TableRegistry` that GraphQL serves. Discovery never queries the warehouse: the manifest is the single source of truth.

- Builds a **bidirectional adjacency list** at construction time by walking the registry: every `RelationDef` on a column becomes two edges (outgoing from the owning table, incoming on the target).
- `find_path()` runs BFS level-by-level, returning *all* shortest paths so the agent can choose between e.g. `orders → customers` and `orders → payments → customers`.

1-hop adjacency questions ("what does `orders` point at?") are answered by reading `@relation` directives in the `describe_table` SDL slice — there's no separate tool for that. Listing and SDL inspection (`list_tables`, `describe_table`) are not part of `SchemaDiscovery`; they execute against the bundle's GraphQL executable schema (`_tables` / `_sdl(tables: ...)`) so policy pruning runs through the same code path as `/graphql`.

---

## Transport

The MCP server uses **Streamable HTTP transport** (FastMCP's default). It mounts at `/mcp` when `serve.mcp_enabled: true` in `config.yml`, alongside the always-on GraphQL endpoint at `/graphql`. Both share one uvicorn process, one auth middleware, and one connection pool.

MCP input arrives as `POST /mcp`; server-to-client events stream via `GET /mcp` (SSE).

---

## Observability

fastmcp ships native OTel support built on `opentelemetry-api` (a hard dep of fastmcp, no extras required). Every tool call automatically emits a `SERVER` span with RPC semantic conventions (`rpc.system: "mcp"`, `rpc.method`, `rpc.service`) and FastMCP-specific attributes. Spans are no-ops unless a traces endpoint is configured — set `monitoring.traces.endpoint` and `monitoring.traces.protocol` in `config.yml` (see [configuration.md](configuration.md)). Distributed trace propagation via `traceparent`/`tracestate` in MCP request meta is also supported.

Each tool call also records three metrics, all labeled with `tool.name` and `status` (`success` / `error`):

| Metric | Type | Unit | Notes |
|---|---|---|---|
| `mcp.tool.calls` | counter | `1` | Total tool invocations. |
| `mcp.tool.duration` | histogram | `ms` | Wall-clock time per call. |
| `mcp.tool.result_bytes` | histogram | `By` | UTF-8 size of the agent-facing payload (string responses) or its JSON serialisation (dict responses). Best-effort — failures to serialise record 0 rather than failing the call. |

Metric records are emitted inside the active span context, so backends that support metric→trace pivoting (Grafana/Tempo, Datadog, Honeycomb) can jump from a slow data point to the originating trace via the shared time window or — once OTel exemplars are enabled on the metric reader — directly via exemplar.

---

## Why MCP-first matters

An HTTP GraphQL endpoint assumes the consumer knows what to ask. An MCP surface assumes the consumer is *learning what to ask*. The latter is the agent workflow.

- GraphJin added MCP in v3 and positions it as the primary agent interface.
- Wren Engine ships an MCP server on top of its MDL.
- dbt-graphql adopts the same pattern — but grounded in dbt artifacts rather than a live DB or a separate modeling language.
