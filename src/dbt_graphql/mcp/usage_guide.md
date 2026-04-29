# dbt-graphql MCP Tools — Usage Guide

## Recommended Workflow

1. **Discover available tables** with `list_tables(filter=None)`.
   Returns the index-page summary of tables visible to the caller — each
   entry has `name`, `description` (dbt-authored), and `tags` (dbt manifest
   tags). Use this to triage candidates *before* drilling in with
   `describe_tables`. The `filter` argument is a case-insensitive substring
   match against name, description, or any tag. Visibility is enforced
   upstream by the GraphQL `_tables` field — denied tables are never
   returned. Structural detail (columns, relations) is intentionally not
   in this view; that's `describe_tables`'s job.

2. **Inspect tables** with `describe_tables(names: [str])`.
   Returns the effective `db.graphql` SDL slice for the named tables, with
   full custom directives (`@table`, `@column`, `@relation`, `@lineage`,
   `@masked`, `@filtered`) — the format the schema is authored in. Names
   the caller cannot see (denied by policy or nonexistent) are silently
   skipped, so the response shape cannot be used to probe for existence.
   **Do not use GraphQL `__schema` introspection** — `describe_tables`
   is the authoritative effective view with full directive metadata.

3. **Find how tables connect** with `explore_relationships(table_name)` or
   `find_path(from_table, to_table)`. These return foreign-key relationships
   you can use to construct multi-table queries.

4. **Trace a column's origin** with `trace_column_lineage(table, column)`.
   Returns upstream sources and downstream consumers from the dbt manifest,
   each tagged with a `lineage_type` (`pass_through`, `rename`,
   `transformation`). Edges to tables you cannot see are stripped.
   The same lineage is also surfaced inline in the SDL via the `@lineage`
   directive: at type level (`@lineage(sources: [...])`) for upstream
   model names, and as a repeatable field-level directive
   (`@lineage(source, column, type)`) for column-level edges.

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

- **Column-level**: `describe_tables` omits blocked columns from the SDL;
  `build_query` strips them from generated queries; `run_graphql` rejects
  them with `FORBIDDEN_COLUMN`.
- **Row-level**: `run_graphql` silently injects row filters into every
  relevant table's resolver. You cannot bypass them.
- **Table-level**: `list_tables`, `describe_tables`, `find_path`,
  `explore_relationships`, `trace_column_lineage` all filter their output
  to authorized tables only. `describe_tables` silently skips unauthorized
  names (same shape as nonexistent names) so the caller cannot probe for
  table existence.

## `build_query` Generates Editable Templates

`build_query` returns a query string you own and can modify before passing
it to `run_graphql`. You can:

- Add `where` arguments for nested relation filtering.
- Request fields on related tables (`has_many` / `has_one`).
- Rename field aliases for clarity.

The tool validates the base query against the schema; once you edit it,
`run_graphql` re-validates and returns errors if invalid.
