# API & Compiler

The two runtime components that execute GraphQL queries: the GraphQL→SQL compiler and the HTTP API layer built on top of it.

See [architecture.md](architecture.md) for the design principles that govern both.

---

## Table of contents

- [1. GraphQL → SQL compiler (`compiler/`)](#1-graphql--sql-compiler-compiler)
- [2. GraphQL API layer (`api/`)](#2-graphql-api-layer-api)

---

## 1. GraphQL → SQL compiler (`compiler/`)

[`src/dbt_graphql/compiler/query.py`](../src/dbt_graphql/compiler/query.py)

### What compilation produces

Given a GraphQL field like:

```graphql
{
  orders(limit: 10, where: { status: "completed" }) {
    order_id
    amount
    customer {
      customer_id
      name
    }
  }
}
```

the compiler emits a single SQLAlchemy `Select`:

```sql
SELECT
  _parent.order_id AS order_id,
  _parent.amount   AS amount,
  (SELECT JSON_AGG(JSON_OBJECT('customer_id', child.customer_id, 'name', child.name))
     FROM customers AS child
     WHERE child.customer_id = _parent.customer_id) AS customer
FROM orders AS _parent
WHERE _parent.status = 'completed'
LIMIT 10;
```

### Why correlated subqueries, not LATERAL joins

Apache Doris (and some older warehouse engines) do not support `LATERAL`. A correlated subquery is portable everywhere and the optimizer collapses it to the same plan on engines that could use LATERAL anyway. The tradeoff is that ordering/limit on nested fields is harder to express; the current compiler doesn't expose those knobs, which is a conscious scope decision.

### Dialect-aware JSON aggregation

Different engines have different JSON aggregation functions. Rather than branching in Python, we define marker classes `json_agg` and `json_build_obj` (`FunctionElement` subclasses) and register per-dialect `@compiles` functions:

| Dialect       | `json_agg`         | `json_build_obj`     |
|---------------|--------------------|----------------------|
| PostgreSQL    | `JSONB_AGG`        | `JSONB_BUILD_OBJECT` |
| MySQL/MariaDB | `JSON_ARRAYAGG`    | `JSON_OBJECT`        |
| SQLite        | `JSON_GROUP_ARRAY` | `JSON_OBJECT`        |
| DuckDB        | `LIST`             | `JSON_OBJECT`        |
| default       | `JSON_ARRAYAGG`    | `JSON_OBJECT`        |

SQL generation stays dialect-agnostic until the moment of rendering.

### `compile_query()` walkthrough

Inputs: a `TableDef`, the GraphQL field node list, the `TableRegistry`, plus optional `limit` / `offset` / `where` / `max_depth`.

1. `_extract_scalar_fields()` partitions the selection into direct columns and FK-backed relations.
2. For each relation: `_build_correlated_subquery` builds a correlated subquery that aggregates child rows into a JSON array, correlated on the FK equality.
3. **Multi-hop nesting** — `_build_correlated_subquery` is recursive. Each level gets a unique alias (`child_1`, `child_2`, …). A `visited` frozenset prevents any model from appearing twice in the same subquery stack (cycle guard), and `max_depth` (default: unlimited) caps nesting depth.
4. `where` is a flat dict of `{col_name: value}` applied as equality predicates. No operator support today.
5. `LIMIT` and `OFFSET` applied straight through to SQLAlchemy.

**Not supported (explicitly):**
- Filtering or ordering on nested fields.
- Operators beyond `=` in `where`.
- Aggregates, group-by, metrics — that's the job of a semantic layer (Cube, MetricFlow).

### Connection management (`compiler/connection.py`)

`DatabaseManager` owns an async SQLAlchemy 2.0 engine, exposes `execute()` for a `Select` and `execute_text()` for raw SQL, and tracks the dialect name. Two construction paths: pass a raw `db_url` string (DuckDB and any URL not in the config map), or pass a `DbConfig` which runs through `build_db_url()`.

`build_db_url()` maps `config.type` keys to async driver schemes (`aiomysql`, `asyncpg`, `aiosqlite`). SQLite is special-cased (file path in host, no auth). DuckDB connects via a raw `duckdb+duckdb:///path` URL.

No dbt profiles parser — the database configuration is deliberately decoupled from `profiles.yml`. A production serve layer connects differently from a dbt transformation run (different credentials, pooling, network).

---

## 2. GraphQL API layer (`api/`)

[`src/dbt_graphql/api/app.py`](../src/dbt_graphql/api/app.py)

Starlette + Ariadne, served via `granian` (Rust-based ASGI server). Starlette is a hard dependency of Ariadne, so no extra package is needed for the outer app.

### Assembling the Ariadne schema

`db.graphql` uses standard GraphQL scalars and custom directives. Ariadne needs a clean executable schema without those directives. `_build_ariadne_sdl()`:

1. Parses `db.graphql`.
2. Collects any type names that aren't standard scalars and declares them as `scalar`.
3. Builds a per-table `XxxWhereInput` input type for filtering.
4. Builds a `Query` type with one field per table, each accepting `limit: Int`, `offset: Int`, and `where: XxxWhereInput`.

This keeps `db.graphql` as a *description* of the warehouse; the Ariadne schema is the *executable* schema derived from it at runtime.

### Lifecycle

`create_app()` builds the Starlette app, mounts `/graphql`, and uses an `@asynccontextmanager` lifespan to connect/close the `DatabaseManager`. State that resolvers need (`TableRegistry`, `DatabaseManager`) is attached to `info.context` — never closure-captured, never module-global.

### Resolvers

`api/resolvers.py` registers one resolver per table. Each resolver:

1. Pulls the `TableDef` out of the registry.
2. Calls `compile_query()` with the GraphQL field nodes, `limit`, `offset`, `where` from kwargs.
3. Executes via the `DatabaseManager`, returns rows as dicts.

No N+1 issues — nested relations are resolved inside the same query via the correlated-subquery mechanism.

### Observability

OTel is bundled with `dbt-graphql[api]`. Three layers activate automatically:

- **Starlette** (`opentelemetry-instrumentation-starlette`) — HTTP request spans.
- **Ariadne** (`ariadne.contrib.tracing.opentelemetry.OpenTelemetryExtension`) — GraphQL operation and per-resolver spans.
- **SQLAlchemy** (`opentelemetry-instrumentation-sqlalchemy`) — per-query spans attached to the engine after connect.

Configure via the `monitoring` block in `config.yml` (see [configuration.md](configuration.md)). Each signal (traces, metrics, logs) has its own `endpoint` and `protocol` field. Instrumentation is bootstrapped in `api/monitoring.py` before the app starts.
