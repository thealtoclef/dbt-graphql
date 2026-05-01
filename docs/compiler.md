# GraphQL → SQL Compiler

The core engine that translates a GraphQL selection into a single warehouse SQL `SELECT` statement. Used by both the [GraphQL HTTP layer](graphql.md) (via resolvers) and the MCP server (via `run_graphql`, which re-executes through the same Ariadne schema).

**Source:** [`src/dbt_graphql/compiler/`](../src/dbt_graphql/compiler/)

See [architecture.md](architecture.md) for the design principles that govern this component.

---

## Table of contents

- [1. What compilation produces](#1-what-compilation-produces)
- [2. Unified `compile_query()`](#2-unified-compile_query)
- [3. Why correlated subqueries, not LATERAL joins](#3-why-correlated-subqueries-not-lateral-joins)
- [4. JSON functions for nested relations](#4-json-functions-for-nested-relations)
- [5. Hasura-inspired operator dispatch](#5-hasura-inspired-operator-dispatch)
- [6. Connection management (`compiler/connection.py`)](#6-connection-management-compilerconnectionpy)

---

## 1. What compilation produces

Given a GraphQL field like:

```graphql
{
  orders(where: { status: { _eq: "completed" } }) {
    order_id
    amount
    customer {
      customer_id
      name
    }
  }
}
```

`compile_query()` produces **one** SQLAlchemy `Select`:

```sql
SELECT
  _uq.order_id AS order_id,
  _uq.amount   AS amount,
  (SELECT JSONB_AGG(JSONB_BUILD_OBJECT('customer_id', child_1.customer_id, 'name', child_1.name))
     FROM customers AS child_1
     WHERE child_1.customer_id = _uq.customer_id) AS customer
FROM orders AS _uq
WHERE _uq.status = 'completed';
```

Nested relations are resolved inside the same statement via correlated subqueries — no N+1, no LATERAL. The resolver calls `compile_query()` once, executes the resulting `Select` against the warehouse via `DatabaseManager`, and returns the rows directly.

---

## 2. Unified `compile_query()`

**Source:** [`src/dbt_graphql/compiler/query.py`](../src/dbt_graphql/compiler/query.py)

The compiler is a single function — `compile_query()` — that handles three SQL shapes depending on which fields the caller selected. It replaces the earlier three-function split (`compile_nodes_query`, `compile_aggregate_query`, `compile_group_query`) that was unified in one refactor to simplify the resolver layer and eliminate the two-Select pattern.

### Inputs

`compile_query(tdef, field_nodes, registry, dialect, where, order_by, limit, offset, distinct, resolve_policy)`:

| Parameter | Type | Purpose |
|---|---|---|
| `tdef` | `TableDef` | Target table definition. |
| `field_nodes` | list | GraphQL AST field nodes for the selection. |
| `registry` | `TableRegistry` | For resolving relation targets. |
| `dialect` | `str` | SQLAlchemy dialect name (used for compilation). |
| `where` | `dict \| None` | `{T}Where` filter tree (`_and`/`_or`/`_not` + per-column ops). |
| `order_by` | `list[tuple] \| None` | `[(field_name, direction)]` parsed from `order_by` argument. |
| `limit` | `int \| None` | Row limit. |
| `offset` | `int \| None` | Row offset. |
| `distinct` | `bool \| None` | Add `DISTINCT` to the SELECT. |
| `resolve_policy` | `Callable \| None` | Maps table name → `ResolvedPolicy` for access control. |

### Three output shapes

The function partitions the requested fields into **dimension columns** (real table columns) and **aggregate fields** (inside `_aggregate`), then selects the appropriate SQL shape:

#### Shape 1: Row-only (no aggregates)

```sql
SELECT dim_cols FROM t WHERE ... ORDER BY ... LIMIT ... OFFSET ...
```

Used when the caller selects only real columns and/or relation fields. Supports `order_by`, `limit`, `offset`, and `distinct`. Nested relations are emitted as correlated subqueries in the SELECT list.

#### Shape 2: Aggregates only (no dimensions)

```sql
SELECT count() AS _count, sum(col) AS _sum_price, avg(col) AS _avg_price FROM t WHERE ...
```

Used when the caller selects `_aggregate { ... }` without any dimension columns. All requested aggregate functions are batched into one SELECT — one DB round-trip regardless of how many aggregate sub-fields were selected. If the projection ends up empty (e.g., all columns are arrays), the SELECT falls back to `count`.

#### Shape 3: Dimensions + aggregates (GROUP BY)

```sql
SELECT dim_cols, agg_cols FROM t WHERE ... GROUP BY dim_cols ORDER BY ... LIMIT ... OFFSET ...
```

Used when the caller selects both real columns and `_aggregate`. The GROUP BY columns are auto-derived from the dimension fields. ORDER BY accepts dimension columns and aggregate aliases (e.g., `_count`, `_sum_price`).

### Mutual exclusivity

- `distinct` + aggregates → `ValueError` (DISTINCT with GROUP BY is semantically different from DISTINCT on raw rows).
- Relation fields + aggregates → `ValueError` (correlated subqueries and GROUP BY cannot mix on the same SELECT).

### Steps

1. `_extract_scalar_fields()` partitions the selection into direct columns and FK-backed relations.
2. Policy resolution: `resolve_policy(table_name)` produces a `ResolvedPolicy` governing column access, masks, and row filters.
3. Column validation: all requested columns are checked against the resolved policy — denied columns raise `ColumnAccessDenied` at compile time.
4. Projections: dimension columns are emitted with masking support; aggregate columns are emitted per the `_aggregate` selection set.
5. WHERE clause: `_where_to_clause()` walks the recursive `{T}Where` tree; the policy `row_filter_clause` is appended as an extra predicate.
6. ORDER BY: column-policy check, then `ASC`/`DESC` clauses. Aggregate aliases are ordered via `literal_column(label)`.
7. LIMIT / OFFSET pass straight through to SQLAlchemy.

### Nested relations

`_build_correlated_subquery()` handles nested relation fields. It is recursive — each nesting level gets a unique alias (`child_1`, `child_2`, …). A `visited` frozenset prevents any model from appearing twice in the same subquery stack (cycle guard). When `resolve_policy` is provided, the policy for the target table is evaluated inside the subquery — denying the table, rejecting unauthorized columns, applying masks, and appending row filters.

### Not supported (explicitly)

- Filtering or ordering on nested relation fields.
- Cursor-based pagination (LIMIT/OFFSET only).
- `distinct_on` (PostgreSQL-only; no portable SQLAlchemy form).

---

## 3. Why correlated subqueries, not LATERAL joins

Apache Doris (and some older warehouse engines) do not support `LATERAL`. A correlated subquery is portable everywhere and the optimizer collapses it to the same plan on engines that could use LATERAL anyway. The tradeoff is that ordering/limit on nested fields is harder to express; the current compiler doesn't expose those knobs, which is a conscious scope decision.

---

## 4. JSON functions for nested relations

Nested GraphQL relations (`orders { customer { name } }`) are compiled into correlated subqueries that aggregate child rows into JSON objects. Different engines use different JSON functions for this. Rather than branching in Python, the compiler defines two SQLAlchemy `FunctionElement` subclasses — `json_agg` and `json_build_obj` — and registers per-dialect compilation rules via SQLAlchemy's `@compiles` extension:

| Dialect       | `json_agg` renders as | `json_build_obj` renders as |
|---------------|----------------------|-----------------------------|
| PostgreSQL    | `JSONB_AGG`          | `JSONB_BUILD_OBJECT`        |
| MySQL/MariaDB | `JSON_ARRAYAGG`      | `JSON_OBJECT`               |
| default       | `JSON_ARRAYAGG`      | `JSON_OBJECT`               |

There is **no Python-level `if dialect == ...` branching** — SQLAlchemy itself dispatches to the correct compilation rule at render time. The query builder stays completely dialect-agnostic. This is the standard SQLAlchemy-native mechanism for dialect-specific SQL generation.

---

## 5. Hasura-inspired operator dispatch

The operator vocabulary (`_eq`, `_neq`, `_gt`, `_gte`, `_lt`, `_lte`, `_in`, `_nin`, `_is_null`, `_like`, `_nlike`, `_ilike`, `_nilike`, `_regex`, `_iregex`) is inspired by Hasura's comparison DSL and shared across two call sites: the GraphQL `{T}Where` filter and the access-policy `row_filter` DSL. Both go through the same `apply_comparison(col, op, value)` function.

**Source:** [`src/dbt_graphql/compiler/operators.py`](../src/dbt_graphql/compiler/operators.py) — `apply_comparison()` translates operator names to SQLAlchemy `ColumnElement` clauses.

**Constants:** [`src/dbt_graphql/schema/constants.py`](../src/dbt_graphql/schema/constants.py) — `COMPARISON_OPS`, `SCALAR_FILTER_OPS`, `LIST_OPS`, `LOGICAL_OPS` define which operators exist and which are valid per GraphQL scalar type. These constants drive both the SDL generator (which emits the `{T}Where` input types) and the compiler (which validates and compiles filter trees).

The split is deliberate: `compiler/operators.py` is runtime dispatch (Python function → SQLAlchemy expression); `schema/constants.py` is static metadata (which ops exist, which scalars accept which ops). Both are consumed by the compiler and the GraphQL layer.

---

## 6. Connection management (`compiler/connection.py`)

**Source:** [`src/dbt_graphql/compiler/connection.py`](../src/dbt_graphql/compiler/connection.py)

`DatabaseManager` owns an async SQLAlchemy 2.0 engine, exposes `execute()` for a `Select` and `execute_text()` for raw SQL, and tracks the dialect name. Two construction paths: pass a raw `db_url` string, or pass a `DbConfig` which runs through `build_db_url()`.

`build_db_url()` maps `config.type` keys to async driver schemes:

| `config.type` | SQLAlchemy scheme |
|---|---|
| `postgres`, `postgresql` | `postgresql+asyncpg` |
| `mysql`, `mariadb`, `doris` | `mysql+aiomysql` |

No dbt profiles parser — the database configuration is deliberately decoupled from `profiles.yml`. A production serve layer connects differently from a dbt transformation run (different credentials, pooling, network).
