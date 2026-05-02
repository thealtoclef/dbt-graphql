# Cursor-Based Pagination

dbt-graphql uses **forward-only cursor-based pagination** — a simplified version of the [Relay Cursor Connections Specification](https://relay.dev/graphql/connections.htm). It omits features that are expensive or unnecessary for data-warehouse and LLM-agent workloads.

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
| `order_by` | `{T}OrderBy` | Defines sort order AND cursor columns. Must form a unique key (PK, all unique columns, or GROUP BY dimensions). Required when using `after` or selecting `pageInfo`. All order_by columns must be selected in `nodes { ... }`. |
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

### Format

Cursors are **base64url-encoded JSON**. There is no HMAC or signature — cursors are stateless and not replay-protected. Row-level security is enforced as separate AND clauses in the SQL `WHERE`, so a forged cursor cannot bypass row filters.

Decoded payload structure:

```json
{
  "p": [["created_at", "desc", "2024-01-15T00:00:00"], ["id", "asc", 42]],
  "w": {"status": {"_eq": "active"}}
}
```

| Key | Contents |
|---|---|---|
| `p` | The `order_by` entries as `[[column, direction, value], ...]`. Each entry pairs a column+direction with the last row's value for that column. Used to validate the cursor spec and to rebuild the chained-OR predicate on the next page. |
| `w` | The `where` filter that was applied when the cursor was created. If a subsequent query uses a different `where`, pagination would return wrong results, so the cursor is invalidated. |

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
        "endCursor": "eyJwIjogW1siY3JlYXRlZF9hdCIsICJkZXNjIl0sIFsiaWQiLCAiYXNjIl1dLCAidiI6IHsiY3JlYXRlZF9hdCI6ICIyMDI0LTAyLTI4VDAwOjAwOjAwIiwgImlkIjogOTk4fX0",
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
    after: "eyJwIjogW1siY3JlYXRlZF9hdCIsICJkZXNjIl0sIFsiaWQiLCAiYXNjIl1dLCAidiI6IHsiY3JlYXRlZF9hdCI6ICIyMDI0LTAyLTI4VDAwOjAwOjAwIiwgImlkIjogOTk4fX0"
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

### `order_by` must use unique columns for stable pagination

dbt-graphql validates that `order_by` columns form a unique key. If not, pagination raises `ORDER_BY_NOT_UNIQUE`:

```graphql
# Error — duplicate created_at values cause unstable pagination
order_by: [{created_at: desc}]

# Valid — id is unique
order_by: [{created_at: desc}, {id: asc}]

# Also valid — uses all primary key columns
order_by: [{customer_id: asc}, {order_id: asc}]
```

Validation succeeds if:
1. `order_by` columns cover all primary key columns (composite PK), OR
2. Every `order_by` column is marked `@unique` in the schema, OR
3. All `order_by` columns are GROUP BY dimensions (aggregate queries)

### Cursor includes WHERE filter

The cursor encodes the `where` filter that was active when it was created. If you change `where` between pages, pagination raises `CURSOR_WHERE_MISMATCH`. This prevents subtle bugs where different filters would return different rows at the same cursor position.

### Cursor includes `order_by` spec

The cursor encodes the `order_by` columns that were active when it was created. If you change `order_by` between pages, pagination raises `CURSOR_ORDER_BY_MISMATCH`.

### NULL values in `order_by` columns

Rows with `NULL` in any `order_by` column are **excluded** by the chained-OR cursor predicate (since `NULL < value` and `NULL = value` evaluate to `NULL`, not `TRUE`). On warehouse tables, `order_by` columns are typically `NOT NULL`. NULL-safe branches may be added in a future release.

### Aggregate queries and pagination

Cursor pagination applies to **row-level queries only**. Selecting `_aggregate` alongside dimension columns (GROUP BY shape) works, but cursor pagination is not supported for aggregate-only queries — cursor values require row-level ordering.

### Cursors are not replay-protected

Cursors are opaque to clients but **decodable by anyone** (base64 + JSON). Row-level security is enforced as separate AND clauses in the SQL `WHERE`, so a forged cursor cannot bypass row filters. The `order_by` and WHERE mismatch checks prevent clients from using a cursor with different parameters.

### No `totalCount`

`COUNT(*)` on billion-row fact tables is a full table scan. If you need a count, use `_aggregate { count }` on a filtered query, but be aware of the cost on large tables.
