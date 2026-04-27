# GraphQL → SQL Compiler

The core engine that translates a GraphQL selection into a single warehouse SQL statement. Used by both the [GraphQL HTTP layer](graphql.md) (via resolvers) and the MCP server (via `execute_query`).

**Source:** [`src/dbt_graphql/compiler/`](../src/dbt_graphql/compiler/)

See [architecture.md](architecture.md) for the design principles that govern this component.

---

## Table of contents

- [1. What compilation produces](#1-what-compilation-produces)
- [2. Why correlated subqueries, not LATERAL joins](#2-why-correlated-subqueries-not-lateral-joins)
- [3. Dialect-aware JSON aggregation](#3-dialect-aware-json-aggregation)
- [4. `compile_query()` walkthrough](#4-compile_query-walkthrough)
- [5. Connection management (`compiler/connection.py`)](#5-connection-management-compilerconnectionpy)

---

## 1. What compilation produces

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

---

## 2. Why correlated subqueries, not LATERAL joins

Apache Doris (and some older warehouse engines) do not support `LATERAL`. A correlated subquery is portable everywhere and the optimizer collapses it to the same plan on engines that could use LATERAL anyway. The tradeoff is that ordering/limit on nested fields is harder to express; the current compiler doesn't expose those knobs, which is a conscious scope decision.

---

## 3. Dialect-aware JSON aggregation

Different engines have different JSON aggregation functions. Rather than branching in Python, we define marker classes `json_agg` and `json_build_obj` (`FunctionElement` subclasses) and register per-dialect `@compiles` functions:

| Dialect       | `json_agg`         | `json_build_obj`     |
|---------------|--------------------|----------------------|
| PostgreSQL    | `JSONB_AGG`        | `JSONB_BUILD_OBJECT` |
| MySQL/MariaDB | `JSON_ARRAYAGG`    | `JSON_OBJECT`        |
| default       | `JSON_ARRAYAGG`    | `JSON_OBJECT`        |

SQL generation stays dialect-agnostic until the moment of rendering.

---

## 4. `compile_query()` walkthrough

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

---

## 5. Connection management (`compiler/connection.py`)

`DatabaseManager` owns an async SQLAlchemy 2.0 engine, exposes `execute()` for a `Select` and `execute_text()` for raw SQL, and tracks the dialect name. Two construction paths: pass a raw `db_url` string, or pass a `DbConfig` which runs through `build_db_url()`.

`build_db_url()` maps `config.type` keys to async driver schemes (`aiomysql`, `asyncpg`).

No dbt profiles parser — the database configuration is deliberately decoupled from `profiles.yml`. A production serve layer connects differently from a dbt transformation run (different credentials, pooling, network).
