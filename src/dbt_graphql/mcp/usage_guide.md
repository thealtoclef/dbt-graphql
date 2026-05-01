# dbt-graphql MCP Tools — Usage Guide

## Recommended Workflow

1. **Discover available tables** with `list_tables()`.
   Returns the index-page summary of tables visible to the caller — each
   entry has `name` and `description` (dbt-authored). Use this to triage
   candidates *before* drilling in with `describe_table`. Visibility is
   enforced upstream by the GraphQL `_tables` field — denied tables are
   never returned. Structural detail (columns, relations) is intentionally
   not in this view; that's `describe_table`'s job. Filter the returned
   list client-side if you need to narrow it.

2. **Inspect tables** with `describe_table(table: str)`.
   Returns the effective `db.graphql` SDL slice for the named table, with
   full custom directives (`@table`, `@column`, `@relation`, `@lineage`,
   `@masked`, `@filtered`) — the format the schema is authored in. Names
   the caller cannot see (denied by policy or nonexistent) are silently
   skipped, so the response shape cannot be used to probe for existence.
   **Do not use GraphQL `__schema` introspection** — `describe_table`
   is the authoritative effective view with full directive metadata.

   `@relation` directives in the SDL describe each foreign-key edge an
   agent can follow when composing nested queries. For most "how does X
   connect?" questions, reading the SDL is enough — `find_path` is the
   tool for the cases SDL alone can't answer.

3. **Find multi-hop join paths** with `find_path(from_table, to_table)`.
   BFS over the relationship graph; returns *all* shortest paths so the
   agent can pick between alternatives. Use this when 1-hop information
   from `describe_table` SDL isn't enough.

4. **Trace a column's origin** with `trace_column_lineage(table, column)`.
   Returns upstream sources and downstream consumers from the dbt manifest,
   each tagged with a `lineage_type` (`pass_through`, `rename`,
   `transformation`). Edges to tables you cannot see are stripped.
   The same upstream lineage is also surfaced inline in the SDL via the
   `@lineage` directive (type-level `@lineage(sources: [...])` and
   field-level `@lineage(source, column, type)`); the dedicated tool
   additionally surfaces *downstream* consumers, which SDL alone does not.

5. **Execute** with `run_graphql(query, variables=None, validate_only=False)`.
   Runs through the same engine as the HTTP endpoint, so column
   allow-lists, masks, and row filters all apply uniformly. Pass
   `validate_only=true` to parse and validate the candidate query
   (including depth, field-count, and list-pagination guards) without
   executing — useful for verifying a query before committing to a real
   run. On success returns `{validation: "ok"}`; on failure returns
   `{errors: [...]}` with the same shape as a normal execution error.

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

- **Column-level**: `describe_table` omits blocked columns from the SDL;
  `run_graphql` rejects them with `FORBIDDEN_COLUMN`.
- **Row-level**: `run_graphql` silently injects row filters into every
  relevant table's resolver. You cannot bypass them.
- **Table-level**: `list_tables`, `describe_table`, `find_path`,
  `trace_column_lineage` all filter their output to authorized tables
  only. `describe_table` silently skips unauthorized names (same shape
  as nonexistent names) so the caller cannot probe for table existence.
