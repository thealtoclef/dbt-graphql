# GraphQL → SQL Compiler

The core engine that translates a GraphQL selection into a single warehouse SQL statement. Used by both the [GraphQL HTTP layer](graphql.md) (via resolvers) and the MCP server (via `run_graphql`, which re-executes through the same Ariadne schema).

**Source:** [`src/dbt_graphql/compiler/`](../src/dbt_graphql/compiler/)

See [architecture.md](architecture.md) for the design principles that govern this component.

---

## Table of contents

- [1. What compilation produces](#1-what-compilation-produces)
- [2. Why correlated subqueries, not LATERAL joins](#2-why-correlated-subqueries-not-lateral-joins)
- [3. Dialect-aware JSON aggregation](#3-dialect-aware-json-aggregation)
- [4. Three compilers (`compile_nodes_query`, `compile_aggregate_query`, `compile_group_query`)](#4-three-compilers)
- [5. Hasura-vocab dispatch (`sql_ops.py`)](#5-hasura-vocab-dispatch-sql_opspy)
- [6. Connection management (`compiler/connection.py`)](#6-connection-management-compilerconnectionpy)

---

## 1. What compilation produces

Given a GraphQL field like:

```graphql
{
  orders(where: { status: { _eq: "completed" } }) {
    nodes(order_by: [{ amount: desc }], limit: 10) {
      order_id
      amount
      customer {
        customer_id
        name
      }
    }
    count
    sum_amount
  }
}
```

the resolver chain produces **two** SQLAlchemy `Select`s — one for `nodes`, one batched aggregate for all sibling aggregate fields:

```sql
-- nodes
SELECT
  _parent.order_id AS order_id,
  _parent.amount   AS amount,
  (SELECT JSON_AGG(JSON_OBJECT('customer_id', child.customer_id, 'name', child.name))
     FROM customers AS child
     WHERE child.customer_id = _parent.customer_id) AS customer
FROM orders AS _parent
WHERE _parent.status = 'completed'
ORDER BY _parent.amount DESC
LIMIT 10;

-- aggregates (one round-trip, all count + sum_* columns the table exposes)
SELECT count(*) AS count, sum(_agg.amount) AS sum_amount, ...
FROM orders AS _agg
WHERE _agg.status = 'completed';
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

## 4. Three compilers

`compile_nodes_query`, `compile_aggregate_query`, and `compile_group_query` share the same WHERE / policy plumbing but differ in the SELECT shape. `compile_query` is kept as a thin alias for `compile_nodes_query` to preserve backward compatibility with callers that don't need the envelope.

### `compile_nodes_query`

Inputs: a `TableDef`, the GraphQL field node list, the `TableRegistry`, plus optional `where` (Hasura `_bool_exp` dict), `order_by` (list of single-key dicts), `limit`, `offset`, `max_depth`, `resolve_policy`.

1. `_extract_scalar_fields()` partitions the selection into direct columns and FK-backed relations.
2. For each relation: `_build_correlated_subquery` builds a correlated subquery that aggregates child rows into a JSON array, correlated on the FK equality.
3. **Multi-hop nesting** — `_build_correlated_subquery` is recursive. Each level gets a unique alias (`child_1`, `child_2`, …). A `visited` frozenset prevents any model from appearing twice in the same subquery stack (cycle guard), and `max_depth` (default: unlimited) caps nesting depth.
4. `where` walks the recursive bool_exp tree (`_and`/`_or`/`_not` plus per-column comparison ops); each column referenced is checked against the resolved column-level policy so a caller can't probe hidden columns via boolean side-channels.
5. `order_by` applies the same column-policy check before emitting `ASC`/`DESC` clauses.
6. Policy `row_filter_clause` (set by the policy engine, JWT-bound) is appended as an extra `WHERE`.
7. `LIMIT` and `OFFSET` go straight through to SQLAlchemy.

### `compile_aggregate_query`

Inputs: a `TableDef`, a `requested_agg_fields` set (e.g. `{"count", "sum_Total"}`), `where`, `resolve_policy`. Emits `SELECT count(*)/sum(col)/...`. Resolvers always pass the *full* table aggregate set so the result can be cached on the carrier dict and shared across siblings — the marginal cost of a few extra aggregates is far less than per-field round-trips. Unsupported names (unknown column, array column) are silently dropped; if the projection ends up empty, the SELECT falls back to `count`.

### `compile_group_query`

Inputs: a `TableDef`, the GraphQL field nodes for `{T}_group`'s selection set, `where`, flat `order_by`, `limit`, `offset`, `resolve_policy`. Cube-style: GROUP BY columns are auto-derived from whichever real column names appear in the selection. The dimension/aggregate split is by set membership against the table's column list — `count`, `sum_<col>` etc. cannot collide with real columns thanks to the boot-time guard in `create_graphql_subapp`. ORDER BY accepts either a dimension column or an aggregate alias (`count`, `sum_Total`); aggregates are emitted via `literal_column(label)` so the underlying engine can ORDER BY the projection alias.

**Not supported (explicitly):**
- Filtering or ordering on nested relation fields.
- Cursor-based pagination (LIMIT/OFFSET only).
- `distinct_on` (PostgreSQL-only; no portable SQLAlchemy form).

---

## 5. Hasura-vocab dispatch (`sql_ops.py`)

`src/dbt_graphql/sql_ops.py` is the single source of truth for translating Hasura comparison operators (`_eq`, `_neq`, `_gt`, `_in`, `_nin`, `_is_null`, `_like`, `_ilike`, …) to SQLAlchemy clauses. Both the GraphQL `{T}_bool_exp` compiler and the policy `row_filter` DSL go through `apply_comparison(col, op, value)` — same op names, same SQL semantics, same NULL handling. The module lives at the package root (not under `compiler/`) to avoid a circular import (`compiler/__init__` → `query` → `graphql.policy` → `graphql.row_filter`).

---

## 6. Connection management (`compiler/connection.py`)

`DatabaseManager` owns an async SQLAlchemy 2.0 engine, exposes `execute()` for a `Select` and `execute_text()` for raw SQL, and tracks the dialect name. Two construction paths: pass a raw `db_url` string, or pass a `DbConfig` which runs through `build_db_url()`.

`build_db_url()` maps `config.type` keys to async driver schemes (`aiomysql`, `asyncpg`).

No dbt profiles parser — the database configuration is deliberately decoupled from `profiles.yml`. A production serve layer connects differently from a dbt transformation run (different credentials, pooling, network).
