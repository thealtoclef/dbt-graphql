# Cursor-Based Pagination

dbt-graphql uses **forward-only cursor-based pagination** — a simplified version of the [Relay Cursor Connections Specification](https://relay.dev/graphql/connections.htm), which is one of the pagination models [recommended by graphql.org](https://graphql.org/learn/pagination/). It omits features that are expensive or unnecessary for data-warehouse and LLM-agent workloads.

**Source:** [`src/dbt_graphql/graphql/cursors.py`](../src/dbt_graphql/graphql/cursors.py), [`src/dbt_graphql/compiler/cursor.py`](../src/dbt_graphql/compiler/cursor.py), [`src/dbt_graphql/graphql/resolvers.py`](../src/dbt_graphql/graphql/resolvers.py)

---

## Table of contents

- [1. How it compares to the full spec](#1-how-it-compares-to-the-full-spec)
- [2. GraphQL API](#2-graphql-api)
- [3. Response shape](#3-response-shape)
- [4. How cursors work](#4-how-cursors-work)
- [5. hasNextPage semantics](#5-hasnextpage-semantics)
- [6. Configuration](#6-configuration)
- [7. Why no last/before](#7-why-no-lastbefore)
- [8. Full example](#8-full-example)
- [9. Limits and caveats](#9-limits-and-caveats)

---

## 1. How it compares to the full spec

| Feature | Relay spec | dbt-graphql | Why |
|---|---|---|---|
| Forward pagination (`first` / `after`) | ✅ | ✅ | Core pagination model |
| Backward pagination (`last` / `before`) | ✅ | ❌ | Reverse `order_by` + `first` covers this; halves implementation surface |
| `Edge` type per table | ✅ | ❌ | `nodes` is simpler — no per-table `CustomerEdge` type |
| `Node` interface | ✅ | ❌ | Irrelevant for warehouse / LLM-agent use case |
| `hasPreviousPage` | ✅ | ❌ | Client tracks cursors from previous pages |
| `totalCount` | ✅ | ❌ | `COUNT(*)` on billion-row fact tables is a full table scan |
| `startCursor` / `endCursor` | ✅ | ❌ / ✅ | `endCursor` only (needed for `after`); `startCursor` unnecessary for forward-only |
| Cursor opaqueness | Encouraged | Opaque but **not HMAC-signed** | Internal tool — row filters are separate AND clauses, so a forged cursor cannot bypass security |

---

## 2. GraphQL API

Each table gets a single root query field with pagination arguments:

```graphql
type Query {
  customers(
    where: CustomerWhere,
    order_by: CustomerOrderBy,
    first: Int,
    after: String,
    distinct: Boolean
  ): CustomerResult!
}
```

### Arguments

| Argument | Type | Description |
|---|---|---|
| `first` | `Int` | Maximum number of rows to return. Defaults to [`query_default_limit`](#6-configuration) (100). Capped at [`query_max_limit`](#6-configuration) (1000). |
| `after` | `String` | Opaque cursor — pass the `endCursor` from a previous page. Requires `order_by`. |
| `order_by` | `{T}OrderBy` | Defines sort order AND cursor columns. For cursor pagination, the **set** of `order_by` columns must contain a unique key — either by covering all PK columns, containing a unique-constraint column, or (for aggregate queries) covering all GROUP BY dimensions. Order within `order_by` does not matter for uniqueness, only for sort direction. Required when using `after` or selecting `pageInfo`. All `order_by` columns must be selected in `nodes { ... }`. See [§9 uniqueness rules](#9-limits-and-caveats). |
| `where` | `{T}Where` | Row-level filters. Applied before pagination. |
| `distinct` | `Boolean` | Deduplicate rows. |

### Behavior matrix

| Arguments | Behavior |
|---|---|
| `first` only (no `after`, no `order_by`) | Plain `LIMIT first`. No pagination metadata (`endCursor` is `null`, `hasNextPage` is `false`). |
| `first` + `order_by` (no `after`) | First page. Returns cursors and `hasNextPage`. |
| `first` + `order_by` + `after` | Subsequent page. Cursor encodes `order_by` column values of the last row on the previous page. |
| `first` + `after` (no `order_by`) | **Error** — `CURSOR_REQUIRES_ORDER_BY`. Cannot decode a cursor without knowing which columns to compare. |
| `pageInfo` selected without `order_by` | **Error** — `ORDER_BY_REQUIRED`. Cursor-based pagination requires `order_by` to define cursor columns. |

---

## 3. Response shape

Every query returns a `{T}Result` wrapper:

```graphql
type CustomerResult {
  nodes: [Customer!]!
  pageInfo: PageInfo!
}
```

The shared `PageInfo` type is emitted once per schema:

```graphql
type PageInfo {
  endCursor: String
  hasNextPage: Boolean!
}
```

- **`nodes`** — the row data. Always a list (empty when no rows match).
- **`pageInfo.endCursor`** — cursor for the last row. Pass this as `after` to fetch the next page. `null` when `nodes` is empty.
- **`pageInfo.hasNextPage`** — `true` when more rows exist beyond this page.

---

## 4. How cursors work

The GraphQL surface exposes Relay-style connections (`first`/`after`/`pageInfo`) for a familiar client experience. Internally, the compiler translates these into **keyset pagination** (the seek method) — a SQL pattern that uses chained-OR predicates to seek directly to the starting row, rather than scanning and discarding rows like `OFFSET`. These are two separate concerns: the API shape (Relay connections) and the execution strategy (keyset pagination).

### Format

Cursors are **base64url-encoded JSON**. There is no HMAC or signature — cursors are stateless and not replay-protected. Row-level security is enforced as separate AND clauses in the SQL `WHERE`, so a forged cursor cannot bypass row filters.

Decoded payload structure:

```json
{
  "d": "a1b2c3d4...full-sha256-hex",
  "v": {"created_at": "2024-01-15T00:00:00", "id": 42}
}
```

| Key | Contents |
|---|---|
| `d` | SHA-256 fingerprint of the query signature (`order_by` + `where` + `distinct` + GROUP BY columns). Used to detect stale cursors — see [§9 cursor stale detection](#9-limits-and-caveats). |
| `v` | Column→value dict from the last row on the page. Used to build the chained-OR predicate for the next page. |

### How the cursor becomes SQL

Given `order_by: [{created_at: desc}, {id: asc}]` with cursor values `{created_at: "2024-01-15", id: 42}`, the compiler generates a **chained-OR WHERE clause**:

```sql
WHERE (created_at < '2024-01-15')
   OR (created_at = '2024-01-15' AND id > 42)
ORDER BY created_at DESC, id ASC
LIMIT 11   -- first=10, +1 for hasNextPage detection
```

Comparison direction matches the direction declared in `order_by`:

| `order_by` direction | Cursor comparison |
|---|---|
| `ASC` | `col > cursor_value` |
| `DESC` | `col < cursor_value` |

With an index on `(created_at DESC, id ASC)`, the database seeks directly to the first matching row — **O(page_size)**, not O(offset + page_size). This is why cursor pagination scales to large tables while offset-based pagination degrades.

---

## 5. hasNextPage semantics

The server fetches `first + 1` rows:

- **`hasNextPage = true`** when more than `first` rows come back. The extra row is discarded. This is **reliable** — the database definitely has more data.
- **`hasNextPage = false`** when exactly `first` rows (or fewer) come back. This means "no evidence of more rows." In standard Relay semantics, `false` is **indeterminate** — it could be the last page, or the result set could be exactly `first` rows long. For practical purposes, stop paginating when `hasNextPage` is `false`.

---

## 6. Configuration

Pagination limits are controlled by two settings in the `graphql` config block:

| Setting | Default | Description |
|---|---|---|
| `query_default_limit` | `100` | Default value for `first` when the client omits it. |
| `query_max_limit` | `1000` | Hard ceiling on `first`. Queries that exceed it silently return fewer rows. Set to `null` to disable the cap. |

In `config.example.yml`:

```yaml
graphql:
  query_default_limit: 100
  query_max_limit: 1000
```

Via environment variables:

```bash
DBT_GRAPHQL__GRAPHQL__QUERY_DEFAULT_LIMIT=100
DBT_GRAPHQL__GRAPHQL__QUERY_MAX_LIMIT=1000
```

**Validation:** `query_default_limit` must be ≤ `query_max_limit` (when `query_max_limit` is set). This is enforced at startup — a misconfigured server will refuse to start.

---

## 7. Why no last/before

The Relay spec supports backward pagination via `last` / `before`, but dbt-graphql omits it:

- **Implementation complexity doubles.** Every code path needs a reverse variant.
- **Reverse pagination is achievable with forward pagination.** To get the last N rows in reverse order, use `order_by: [{col: desc}]` with `first: N`. This produces identical SQL and identical cursor semantics.
- **Clients track their own history.** `hasPreviousPage` is unnecessary — the client already has the cursors from previous pages.

---

## 8. Full example

Fetch the first page of customers ordered by creation date, then paginate:

```graphql
# --- Page 1 ---
query {
  customers(
    order_by: [{created_at: desc}, {id: asc}]
    first: 10
  ) {
    nodes {
      id
      name
      email
      created_at
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
```

Response:

```json
{
  "data": {
    "customers": {
      "nodes": [
        {"id": 999, "name": "Alice", "email": "alice@example.com", "created_at": "2024-03-01"},
        {"id": 998, "name": "Bob", "email": "bob@example.com", "created_at": "2024-02-28"}
      ],
        "pageInfo": {
        "endCursor": "eyJkIjogImFiYy4uLmEgdGhlIGFjdHVhbCBzaGEyNTYgaGV4IHN0cmluZyIsICJ2IjogeyJjcmVhdGVkX2F0IjogIjIwMjQtMDItMjhUMDA6MDA6MDAiLCAiaWQiOiA5OTh9fQ",
        "hasNextPage": true
      }
    }
  }
}
```

```graphql
# --- Page 2 ---
query {
  customers(
    order_by: [{created_at: desc}, {id: asc}]
    first: 10
    after: "eyJkIjogImFiYy4uLmEgdGhlIGFjdHVhbCBzaGEyNTYgaGV4IHN0cmluZyIsICJ2IjogeyJjcmVhdGVkX2F0IjogIjIwMjQtMDItMjhUMDA6MDA6MDAiLCAiaWQiOiA5OTh9fQ"
  ) {
    nodes {
      id
      name
      email
      created_at
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
```

With filtering:

```graphql
query {
  customers(
    where: { status: { _eq: "active" } }
    order_by: [{created_at: desc}, {id: asc}]
    first: 10
  ) {
    nodes {
      id
      name
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
```

---

## 9. Limits and caveats

### `order_by` uniqueness requirement

Cursor pagination uses a **chained-OR predicate** that encodes ALL `order_by` columns into the cursor. The SQL it produces is always correct — it is equivalent to a row-value comparison `(col₁, col₂, …) > (val₁, val₂, …)`. However, pagination requires that **no two rows can tie on every `order_by` column**. If they do, the cursor cannot distinguish between them, and rows may be skipped or duplicated across pages.

dbt-graphql validates this at query time and raises `CURSOR_ORDER_BY_NOT_UNIQUE` when validation fails.

#### Set-based validation

The chained-OR uses ALL `order_by` columns regardless of their position, so **the order of columns in `order_by` does not matter for uniqueness** — only the SET of columns matters. The validator checks whether the set of `order_by` columns contains a unique key.

A set of columns is **unique** when any of:

| # | Condition | Why it works |
|---|---|---|
| 1 | Contains all **PK columns** of the table | PK is unique by definition |
| 2 | Contains at least one column with a **unique constraint** | That column alone distinguishes all rows |
| 3 | (Aggregate queries only) Contains all **GROUP BY dimension columns** | GROUP BY produces one row per unique combination |

Any one condition is sufficient. Order within `order_by` only affects sort direction, not whether pagination is correct.

#### Examples

**Non-aggregate, single PK:**

```graphql
# Table: orders  PK: order_id, unique: surrogate_key

# ✅ order_id is the PK
{ orders(order_by: {order_id: asc}, first: 10) { ... } }

# ✅ order_id is PK — its position doesn't matter
{ orders(order_by: [{status: asc}, {order_id: asc}], first: 10) { ... } }

# ✅ surrogate_key has a unique constraint
{ orders(order_by: {surrogate_key: asc}, first: 10) { ... } }

# ❌ created_at is not unique, no unique column in the set
{ orders(order_by: {created_at: asc}, first: 10) { ... } }
```

**Non-aggregate, composite PK:**

```graphql
# Table: order_items  PK: (order_id, item_id)

# ✅ Both PK columns present
{ order_items(order_by: [{order_id: asc}, {item_id: asc}], first: 10) { ... } }

# ✅ Both PK columns present — order within order_by doesn't matter
{ order_items(order_by: [{status: asc}, {order_id: asc}, {item_id: asc}], first: 10) { ... } }

# ❌ order_id alone is not a complete PK
{ order_items(order_by: {order_id: asc}, first: 10) { ... } }
```

**Aggregate queries (GROUP BY dimensions):**

When `_aggregate` is selected, the compiler groups by all non-aggregate selected columns. Those GROUP BY dimensions form a unique key in the result set.

```graphql
# Selected dimensions: agent_email  →  GROUP BY (agent_email)

# ✅ GROUP BY is (agent_email), order_by covers it
{
  fct(order_by: {agent_email: asc}, first: 5) {
    nodes { agent_email  _aggregate { count } }
    pageInfo { endCursor hasNextPage }
  }
}
```

**Aggregate with multiple dimensions:**

```graphql
# Selected dimensions: agent_email, action  →  GROUP BY (agent_email, action)

# ❌ agent_email alone does not cover all GROUP BY dimensions
{
  fct(order_by: {agent_email: asc}, first: 5) {
    nodes { agent_email  action  _aggregate { count } }
    pageInfo { endCursor hasNextPage }
  }
}

# ✅ Both dimensions covered
{
  fct(order_by: [{agent_email: asc}, {action: asc}], first: 5) {
    nodes { agent_email  action  _aggregate { count } }
    pageInfo { endCursor hasNextPage }
  }
}

# ✅ surrogate_key has a unique constraint — no need for GROUP BY coverage
{
  fct(order_by: {surrogate_key: asc}, first: 5) {
    nodes { agent_email  action  _aggregate { count } }
    pageInfo { endCursor hasNextPage }
  }
}
```

#### Error messages

When validation fails, the error lists actionable hints — what columns to **include** in `order_by`:

```
order_by does not guarantee a stable cursor — columns do not form a unique key.
Got: ['created_at']. To fix, include PK columns ['order_id']; or include any
unique-constraint column ['surrogate_key'].
```

```
order_by does not guarantee a stable cursor — columns do not form a unique key.
Got: ['agent_email']. To fix, include PK columns ['surrogate_key']; or include
all GROUP BY dimension columns ['action', 'agent_email'] (missing: ['action']).
```

#### Validation flow

```
order_by provided?
  │
  no ──→ skip validation (plain LIMIT, no cursors)
  │
  yes ──→ build set of order_by columns
          │
          ├─ set ⊇ all PK columns? ──→ ✅ valid
          ├─ set ∩ unique-constraint columns ≠ ∅? ──→ ✅ valid
          ├─ set ⊇ all GROUP BY dims (aggregate only)? ──→ ✅ valid
          │
          └─ none of the above ──→ ❌ CURSOR_ORDER_BY_NOT_UNIQUE
```

### Data mutations between pages

Because cursors are stateless (no server-side session), INSERTs, UPDATEs, or DELETEs that occur between pages can cause rows to appear shifted. This is inherent to all stateless keyset pagination and cannot be prevented. For static or append-only warehouse tables this is rarely an issue.

### Cursor stale detection

The cursor encodes a SHA-256 fingerprint of all query parameters that affect which rows appear and in what order. If any of these change between pages, the cursor is rejected with `CURSOR_STALE` — start a fresh query without `after`.

#### Parameters covered by the digest

| Parameter | Why it matters |
|---|---|
| `order_by` | Sort order determines cursor position. Different sort → different rows at the same position. |
| `where` | Filter changes the result set. A cursor pointing at row N in filtered set A is meaningless in filtered set B. |
| `distinct` | `DISTINCT` collapses duplicate rows, changing row count. A cursor created without `distinct` may point at a row that was deduplicated away with `distinct: true`. |
| Selected fields (with `_aggregate`) | When `_aggregate` is selected, the compiler groups by all non-aggregate selected columns. Changing which dimension columns are selected changes the GROUP BY, which changes the result set shape and row count. |

#### Parameters NOT covered (by design)

| Parameter | Why it's safe to change |
|---|---|
| `first` | Only controls page size. A different `first` on page 2 returns a different number of rows but starts at the correct cursor position. |
| Selected fields (without `_aggregate`) | Only changes column projection. The same rows are returned in the same order — cursor position is unaffected. |
| Table name | Each table has its own resolver. You can't send an `orders` cursor to a `customers` query. |

### NULL values in `order_by` columns

Rows with `NULL` in any `order_by` column are **excluded** by the chained-OR cursor predicate (since `NULL < value` and `NULL = value` evaluate to `NULL`, not `TRUE`). On warehouse tables, `order_by` columns are typically `NOT NULL`. NULL-safe branches may be added in a future release.

### Aggregate queries and pagination

Cursor pagination applies to **row-level queries only**. Selecting `_aggregate` alongside dimension columns (GROUP BY shape) works, but cursor pagination is not supported for aggregate-only queries — cursor values require row-level ordering.

### Cursors are not replay-protected

Cursors are opaque to clients but **decodable by anyone** (base64 + JSON). Row-level security is enforced as separate AND clauses in the SQL `WHERE`, so a forged cursor cannot bypass row filters. The `order_by` and WHERE mismatch checks prevent clients from using a cursor with different parameters.

### No `totalCount`

`COUNT(*)` on billion-row fact tables is a full table scan. If you need a count, use `_aggregate { count }` on a filtered query, but be aware of the cost on large tables.
