# GraphQL HTTP API

The HTTP interface for running GraphQL queries against the warehouse. Built on Starlette + Ariadne, served via uvicorn.

**Source:** [`src/dbt_graphql/graphql/`](../src/dbt_graphql/graphql/) (sub-app + resolvers + auth + policy) and [`src/dbt_graphql/serve/`](../src/dbt_graphql/serve/) (Starlette composition + uvicorn runner)

The SQL compilation engine lives in [compiler.md](compiler.md). This document covers the HTTP layer: schema assembly, lifecycle, resolvers, auth, and observability.

See [architecture.md](architecture.md) for the design principles that govern this component.

---

## Table of contents

- [1. Schema assembly (`_build_ariadne_sdl`)](#1-schema-assembly-_build_ariadne_sdl)
- [2. Lifecycle](#2-lifecycle)
- [3. Resolvers](#3-resolvers)
- [4. Auth](#4-auth)
- [5. Observability](#5-observability)
- [6. Co-mounting with MCP](#6-co-mounting-with-mcp)

---

## 1. Schema assembly (`_build_ariadne_sdl`)

`db.graphql` uses custom directives (`@table`, `@column`, `@relation`, `@unique`, `@masked`, `@filtered`) that Ariadne's schema builder doesn't understand. At serve time, `_build_ariadne_sdl(registry: TableRegistry)` derives a clean executable schema directly from the `TableRegistry`.

The schema exposes **one root field per table** that returns a flat list of row objects — no envelope. For each `TableDef` it emits:

- **`type {T}`** — the row type. One field per column, plus a `_aggregate` field for inline aggregates and relation fields for FK-backed columns.
- **`type {T}_aggregate`** — aggregate wrapper with per-function sub-objects:
  - `count: Int!`
  - `sum: {T}_aggregate_sum` — numeric columns.
  - `avg: {T}_aggregate_avg` — numeric columns (always `Float`).
  - `stddev: {T}_aggregate_stddev` / `var: {T}_aggregate_var` — numeric columns.
  - `count_distinct: {T}_aggregate_count_distinct` — all scalar columns.
  - `min: {T}_aggregate_min` / `max: {T}_aggregate_max` — all scalar columns.
- **Per-operation aggregate types** (`{T}_aggregate_sum`, `{T}_aggregate_avg`, etc.) — named types containing the relevant columns as fields.
- **`input {T}Where`** — recursive Hasura-style WHERE filter. `AND` / `OR` / `NOT` plus per-column typed filter inputs.
- **`input {T}OrderBy`** — per-column ordering. Each column and `_aggregate` map to `OrderDirection`.
- **`enum {T}Column`** — one value per scalar column.

The query field is:

```graphql
{T}(where: {T}Where, order_by: {T}OrderBy, limit: Int, offset: Int, distinct: Boolean): [{T}!]!
```

Pagination (`limit`, `offset`), ordering (`order_by`), and deduplication (`distinct`) are arguments on the root field. `where` filters rows; the same filter applies to aggregates since they are computed over the same result set.

### Shared types emitted once per schema

- **`StringFilter`**, **`IntFilter`**, **`FloatFilter`**, **`BooleanFilter`** — per-scalar comparison inputs containing the valid operators for that type (e.g., `StringFilter` includes `_like`, `_ilike`, `_regex`; `IntFilter` does not).
- **`enum OrderDirection { asc desc }`** — direction for order-by fields.

### Aggregate batching

Selecting `_aggregate` alongside dimension columns compiles into a single SELECT with GROUP BY — one DB round-trip. Selecting `_aggregate` alone compiles into a bare aggregate SELECT. Either way, all requested aggregate functions (`count`, `sum { price quantity }`, `avg { price }`, etc.) are batched into one statement.

### Example query

```graphql
{
  orders(where: {
    _and: [
      { status: { _in: ["completed", "shipped"] } },
      { _or: [{ amount: { _gte: 100 } }, { vip: { _eq: true } }] }
    ]
  }) {
    order_id
    amount
    status
    customer { customer_id name }
    _aggregate {
      count
      sum { amount }
      avg { amount }
    }
  }
}
```

**`TableRegistry` is the input — not `db.graphql`.** The serve path never reads or parses a file; it operates on the Python object built by `build_registry()` directly from dbt artifacts.

### Introspection signals

Standard GraphQL `IntrospectionQuery` only exposes a fixed set of fields on `__Type` / `__Field`; applied directives are not in that set. To make policy- and structure-relevant signals visible to GraphiQL / Apollo Studio / codegen, the executable SDL routes them through native introspection carriers wherever possible:

- **Primary keys** keep their underlying scalar (`Int!`, `String!`, …) and carry an `@id` directive in the printed `db.graphql` artefact. Preserving the scalar lets `{T}Where` dispatch the correct `<Scalar>Filter` — int PKs get numeric ops, text/UUID PKs get string ops including `_like`/`_ilike`. The PK signal travels via `@id` (visible to LLM agents through `Query._sdl`); standard `__schema` introspection no longer flags PK-ness, which is fine because no current consumer relies on that signal.
- **dbt descriptions** on tables and columns are emitted as triple-quoted blocks above the type / field, so they show up directly in `__Type.description` and `__Field.description`.
- **`@masked` / `@filtered`** are emitted in the printed `db.graphql` artefact when the corresponding flags are set. They will be set per principal once policy-aware introspection is wired; today `ColumnDef.masked` / `TableDef.filtered` exist as scaffolding and are not populated at runtime.

The remaining custom directives (`@table`, `@column`, `@id`, `@relation`, `@unique`, `@masked`, `@filtered`) do not appear in standard `__schema` introspection. They are exposed via two dedicated `Query` fields:

- **`_sdl(tables: [String!]): String!`** — the **effective** db.graphql SDL for the current caller, pruned to tables and columns the caller's `AccessPolicy` allows, with `@masked` / `@filtered` injected per the resolved policy. Without `tables`, the full caller-effective document is returned. With `tables`, the output is intersected with the given names; names the caller cannot see (denied by policy or nonexistent) are silently skipped — an unauthorized name and a missing name are indistinguishable to the client by design.
- **`_tables: [_TableInfo!]!`** — the cheap "index page" for the visible surface. Each entry carries `name` and `description` (dbt-authored) — enough for an agent to triage candidates before paying full-SDL cost via `_sdl(tables: [...])`. Structural detail (columns, relations) is intentionally omitted; that's `_sdl`'s job.

The same pruned-AST renderer powers the MCP `describe_table` tool, so HTTP clients and LLM agents see byte-identical SDL. (The `--output` artefact is the unfiltered "boot" view; the per-caller view is `_sdl`.) The names `_sdl`, `_tables`, and `_TableInfo` are reserved — a dbt model colliding with any of them is rejected at boot.

---

## 2. Lifecycle

Composition lives in `serve/app.py:create_app`. `graphql/app.py:create_graphql_subapp` builds only the Ariadne ASGI sub-app — the Starlette assembly, auth middleware, lifespan, and any MCP co-mount are owned by `serve/app.py`. The single `serve/__init__.py:run()` entry point is what the CLI calls.

The `@asynccontextmanager` lifespan:

1. Enters the MCP app's lifespan (if co-mounted) via `AsyncExitStack`.
2. Connects `DatabaseManager` (only if GraphQL is enabled) and instruments the SQLAlchemy engine with OTel.
3. Sets up the result cache (if configured).
4. Yields — uvicorn serves requests.
5. Tears down in reverse order on shutdown.

State that resolvers need (`TableRegistry`, `DatabaseManager`, JWT payload, `PolicyEngine`, `CacheConfig`) is passed through `info.context` — never captured in closures, never module-global.

---

## 3. Resolvers

**Source:** [`src/dbt_graphql/graphql/resolvers.py`](../src/dbt_graphql/graphql/resolvers.py)

`create_query_type(registry)` builds a `QueryType` with one root resolver per table. There are no per-table `ObjectType` bindings — the root field returns rows directly.

Per request, the resolver chain is a single step:

1. **`Query.{T}`** (async) — calls `compile_query()` with the field nodes, `where`, `order_by`, `limit`, `offset`, `distinct`, and the policy resolver. Executes the resulting `Select` via `execute_with_cache()` (result cache + singleflight). Then restructures flat aggregate keys (`_count`, `_sum_price`, etc.) into the nested `{ count: N, sum: { price: X } }` shape that the GraphQL `_aggregate` field expects. Returns the rows directly.

No N+1 — nested relations on `{T}` rows are resolved inside the same SELECT via correlated subqueries (see [compiler.md](compiler.md)).

---

## 4. Auth

JWT verification is handled by the `AuthenticationMiddleware` layer. When `security.enabled: true` in config, every request is verified against the configured key source (JWKS URL, key file, or env var). Verification uses `joserfc` with an explicit algorithm allow-list and configurable `audience`, `issuer`, `leeway`, and `required_claims`. The decoded payload is attached as `request.user.payload` and forwarded to `info.context["jwt_payload"]` for the policy engine.

When JWT is disabled (the default), every request is treated as anonymous — the policy engine sees an empty payload.

See [security.md](security.md) for the full JWT configuration reference and key-source options.

---

## 5. Observability

OTel is bundled with `dbt-graphql`. Four auto-instrumentation layers activate automatically:

- **Starlette/ASGI** (`opentelemetry-instrumentation-starlette`, which wraps `opentelemetry-instrumentation-asgi`) — HTTP request spans, plus the standard ASGI metrics (`http.server.duration`, `http.server.active_requests`, request/response size histograms).
- **httpx** (`opentelemetry-instrumentation-httpx`) — outbound HTTP spans (e.g. JWKS key fetches).
- **Ariadne** (`ariadne.contrib.tracing.opentelemetry.OpenTelemetryExtension`) — GraphQL operation and per-resolver spans.
- **SQLAlchemy** (`opentelemetry-instrumentation-sqlalchemy`) — per-query spans (engine pool depth is auto-emitted via `db.client.connections.usage`).

Additionally, custom metrics are emitted by application code:

- `graphql.operation.count` / `graphql.operation.duration` / `graphql.operation.errors` — per operation, recorded by `GraphQLMetricsExtension`.
- `db.client.connections.wait_time` — pool checkout wait time, recorded in `DatabaseManager`.
- `db.client.queries.count` / `db.client.queries.duration` — per SQL query, recorded in `DatabaseManager`.
- `http.server.responses` — HTTP response count grouped by status code (2xx/4xx/5xx), recorded via the Starlette response hook.
- `jwt.verification.outcomes` — JWT verification results.
- `cache.result.outcomes` — cache hit/miss/error.
- `mcp.tool.calls` / `mcp.tool.duration` — per MCP tool, recorded by the `_instrument_tool` wrapper.

See [configuration.md](configuration.md) for the `monitoring` block reference.

---

## 6. Co-mounting with MCP

GraphQL is always mounted at `/graphql` in serve mode. When `serve.mcp_enabled: true` in config, the MCP HTTP app is co-mounted at `/mcp` under the same Starlette + uvicorn process. The MCP app's lifespan is composed into the Starlette lifespan via `AsyncExitStack`; the same `AuthenticationMiddleware` runs above both mounts and the same OTel instrumentation covers both endpoints.

`create_app` passes the `GraphQLBundle` into the MCP factory so the MCP `run_graphql` tool re-executes queries through the same Ariadne schema with the same per-request context — column allow-lists, masks, and row filters apply structurally to MCP traffic the same way they do to direct GraphQL. See [mcp.md § Authorization model](mcp.md#authorization-model).
