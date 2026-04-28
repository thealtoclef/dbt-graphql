# dbt-graphql MCP Tools — Usage Guide

## Recommended Workflow

1. **Discover available tables** with `list_tables(filter=None)`.
   The `filter` argument does a case-insensitive substring match on table name
   and description, so you can narrow down a large warehouse without loading
   everything into context.

2. **Inspect a table's columns** with `describe_table(name)`.
   Returns column names, types, descriptions, and live-DB sample values — all
   filtered to what your JWT authorizes you to see.

3. **Find how tables connect** with `explore_relationships(table_name)` or
   `find_path(from_table, to_table)`. These return foreign-key relationships
   you can use to construct multi-table queries.

4. **Trace a column's origin** with `trace_column_lineage(table, column)`.
   Returns upstream sources and downstream consumers from the dbt manifest,
   each tagged with a `lineage_type` (`pass_through`, `rename`,
   `transformation`). Edges to tables you cannot see are stripped.

5. **Build a query template** with `build_query(table, fields)`.
   Pass the fields you need; the tool returns a valid GraphQL query string,
   filtered by policy and validated against the live schema.

6. **Execute** with `run_graphql(query, variables=None)`.
   Runs through the same engine as the HTTP endpoint, so column allow-lists,
   masks, and row filters all apply uniformly.

## Query Guards

Every query — whether sent to `/graphql` over HTTP or via `run_graphql` —
is validated against three limits before execution:

- **Depth** — maximum selection-set nesting depth. Default 5.
- **Fields** — maximum total leaf fields in the operation. Default 50.
- **List limit** — caps integer literals on `limit:` / `first:` arguments.
  Default 1000. Variables bypass this rule by design (validation runs
  before binding); the resolver applies a runtime cap when accepting a
  variable for pagination.

Violations return a GraphQL error with `extensions.code` set to
`MAX_DEPTH_EXCEEDED`, `MAX_FIELDS_EXCEEDED`, or `MAX_LIMIT_EXCEEDED`
respectively (HTTP 400).

## Nested Relations via the `where` Argument

When a table has `has_many` relationships (e.g., `orders` → `line_items`),
you can nest them in the query and filter related rows server-side:

```graphql
query {
  customers {
    customer_id
    orders(where: { status: { _eq: "pending" } }) {
      order_id
      status
    }
  }
}
```

The `where` argument accepts boolean expressions (`_and`, `_or`, `_eq`,
`_in`, `_is_null`, …). Omitting `where` returns all related rows.

## Row Filters Are Invisible to the Caller

If an access policy applies row-level filters (e.g.,
`tenant_id = jwt.claims.org_id`), those filters are injected by the server
**before** your query runs. You will not see them in the query string, and
you cannot override them — the server enforces them on every execution.

- You receive only rows your JWT authorizes.
- You cannot accidentally or intentionally query rows outside your policy.
- `run_graphql` returns only data your policy allows.

## JWT-Driven Column and Row Access

Every tool honours the caller's JWT payload. Depending on the deployed
policy:

- **Column-level**: `describe_table` and `build_query` strip blocked
  columns; `run_graphql` rejects them with `FORBIDDEN_COLUMN`.
- **Row-level**: `run_graphql` silently injects row filters into every
  relevant table's resolver. You cannot bypass them.
- **Table-level**: `list_tables`, `describe_table`, `find_path`,
  `explore_relationships`, `trace_column_lineage` all filter their output
  to authorized tables only.

## `build_query` Generates Editable Templates

`build_query` returns a query string you own and can modify before passing
it to `run_graphql`. You can:

- Add `where` arguments for nested relation filtering.
- Request fields on related tables (`has_many` / `has_one`).
- Rename field aliases for clarity.

The tool validates the base query against the schema; once you edit it,
`run_graphql` re-validates and returns errors if invalid.
