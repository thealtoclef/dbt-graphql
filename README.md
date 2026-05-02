# dbt-graphql

Turn a dbt project into a typed GraphQL schema, a SQL-backed GraphQL API, and an MCP surface for LLM agents — without authoring a second modeling layer. dbt-graphql reads `catalog.json` and `manifest.json` and derives everything from what your analytics team already maintains.

## Installation

```bash
pip install dbt-graphql                    # core (generate + serve)
pip install dbt-graphql[postgres]          # + asyncpg
pip install dbt-graphql[mysql]             # + aiomysql
pip install dbt-graphql[redis]             # + Redis-backed cache for multi-replica
pip install dbt-graphql[gcs]               # + read catalog/manifest from gs://
pip install dbt-graphql[s3]                # + read catalog/manifest from s3://
```

## Quick start

Configuration comes from a YAML file (optional, passed via `--config`)
plus `DBT_GRAPHQL__*` environment variables (always read, take precedence).
With `--config` omitted, all settings must come from env vars — handy for
containerised deploys. See [`config.example.yml`](config.example.yml) for
a documented template.

**1. Generate schema files (no DB connection required)**

```bash
dbt-graphql --config config.yml --output ./out
# → out/db.graphql
```

**2. Serve the API**

```bash
dbt-graphql --config config.yml
```

GraphQL is always mounted at `/graphql` in serve mode. Set
`serve.mcp_enabled: true` in `config.yml` to additionally co-mount the
MCP server at `/mcp`. Both transports share one uvicorn process, one
JWT auth middleware, one connection pool, and one access policy — the
MCP `run_graphql` tool runs through the same engine, so column
allow-lists, masks, and row filters apply uniformly to both.

```yaml
# config.yml (excerpt)
serve:
  host: 0.0.0.0
  port: 9876
  mcp_enabled: false             # opt-in; expose MCP tools to LLM agents
  graphql_introspection: false   # off in prod; on for dev tooling
```

## Query layer

Each table exposes one root field that returns a `{T}Result!` envelope with
`nodes` (a flat list of row objects) and `pageInfo` (pagination metadata).
Columns, nested relations, and inline aggregates are selected as sibling
fields inside `nodes` — one `SELECT`, one DB round-trip.

```graphql
{
  orders(
    where: {
      _and: [
        { status: { _in: ["completed", "shipped"] } },
        { _or: [{ amount: { _gte: 100 } }, { vip: { _eq: true } }] }
      ]
    },
    order_by: { amount: desc },
    first: 50
  ) {
    nodes {
      order_id amount status
      customer { customer_id name }   # nested via correlated subquery (no LATERAL)
      _aggregate {
        count
        sum { amount }
        avg { amount }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
```

- **`first`**: maximum rows to return. When `order_by` is absent, acts as a
  plain LIMIT — returns `{T}Result` with `hasNextPage: false` and no cursors.
  When `order_by` is provided with unique columns, cursor pagination is
  enabled and `hasNextPage` reflects whether more rows exist.
- **`after`**: cursor token to resume from a previous page. Requires `order_by`.
- **`{T}Where`** (Hasura-inspired filter): `_and` / `_or` / `_not` plus per-column
  `_eq` / `_neq` / `_gt` / `_gte` / `_lt` / `_lte` / `_in` / `_nin` / `_is_null`
  / `_like` / `_nlike` / `_ilike` / `_nilike`. The same operator set is reused
  by access-policy `row_filter` blocks.
- **`{T}OrderBy`**: per-column ordering with `asc | desc`. Serves as both sort
  order and cursor columns. All order_by columns must be selected in
  `nodes { ... }`. Must form a unique key. Required when using `after` cursors
  or selecting `pageInfo`.
- **`distinct: true`** on the root field adds a plain `DISTINCT` to the SELECT.
- **Aggregates**: nested under `_aggregate { ... }` as a field on `{T}`. `count`
  always; `sum` / `avg` / `stddev` / `var` contain numeric columns;
  `count_distinct` / `min` / `max` contain all scalar columns. Selecting any
  combination fires one batched SELECT per request.
- **Mutual exclusivity**: `distinct` cannot be combined with `_aggregate`. Relation
  fields cannot be combined with `_aggregate` (correlated subqueries and GROUP BY
  do not mix).

WHERE / ORDER BY references to columns the caller's policy hides raise
`FORBIDDEN_COLUMN` at compile time so callers cannot probe hidden values
through boolean side-channels.

See [`docs/graphql.md`](docs/graphql.md) and [`docs/compiler.md`](docs/compiler.md)
for the full SDL shape and SQL generation details.

## Use with LLM Agents (Claude Code, OpenCode)

With `serve.mcp_enabled: true`, the server exposes MCP at `http://<host>:<port>/mcp`
over Streamable HTTP. The `--header` flag is required even for no-auth / dev-mode
servers — agents probe `/.well-known/*` OAuth endpoints on every HTTP MCP
connection; without an explicit header they treat a 404 as auth failure and refuse
to connect. Any header value works; the server ignores it when `dev_mode: true`
or `security.enabled: false`.

Once connected, agents autoload the `dbt-graphql://usage-guide` resource and can
call `list_tables`, `describe_tables`, `find_path`, `trace_column_lineage`, and
`run_graphql` (with optional `validate_only`) against your warehouse — every call
gated by the same `AccessPolicy` as `/graphql`.

### Claude Code

```bash
claude mcp add --transport http dbt-graphql http://localhost:9876/mcp \
  --header "X-No-Auth: true"
# with auth:
claude mcp add --transport http dbt-graphql http://localhost:9876/mcp \
  --header "Authorization: Bearer $JWT"
```

### OpenCode

```bash
opencode mcp add dbt-graphql http://localhost:9876/mcp \
  --header "X-No-Auth: true"
# with auth:
opencode mcp add dbt-graphql http://localhost:9876/mcp \
  --header "Authorization: Bearer $JWT"
```

## Access policy

Per-request RBAC, row filters (Hasura-style structured DSL), and column
masking — declared inline under `security.policies` in `config.yml` and
evaluated at SQL compile time. The single `security.enabled` flag gates
both JWT verification (authn) and policy evaluation (authz):

```yaml
# config.yml — security block
security:
  enabled: true
  jwt:
    algorithms: [RS256]
    jwks_url: https://issuer.example/.well-known/jwks.json
  policies:
    - name: analyst
      effect: allow                    # IAM-style; required, no default
      when: "'analysts' in jwt.groups"
      tables:
        customers:
          column_level:
            include_all: true
            mask:
              email: "CONCAT('***@', SPLIT_PART(email, '@', 2))"
          row_filter:
            org_id: { _eq: { jwt: claims.org_id } }

    # Cross-cutting deny — wins over any allow that also matches.
    - name: contractors_no_pii
      effect: deny
      when: "'contractors' in jwt.groups"
      tables:
        customers: { deny_columns: [email, ssn] }
```

See [`config.example.yml`](config.example.yml) and
[docs/access-policy.md](docs/access-policy.md).

## Documentation

- [**Architecture & Design**](docs/architecture.md) — pipeline, design principles, and landscape comparison.
- [**Schema Synthesis**](docs/schema-synthesis.md) — dbt extraction, IR, SDL generation, and lineage in depth.
- [**GraphQL API**](docs/graphql.md) — sub-app, resolvers, auth, observability.
- [**Compiler**](docs/compiler.md) — GraphQL → SQL with correlated subqueries.
- [**Caching & Burst Protection**](docs/caching.md) — result cache + singleflight between resolver and warehouse.
- [**Access Policy**](docs/access-policy.md) — RBAC, structured row filters, column masking.
- [**Security**](docs/security.md) — JWT verification, threat model, anonymous mode.
- [**Configuration Reference**](docs/configuration.md) — operator-facing config surface and env-var precedence.
- [**MCP Server**](docs/mcp.md) — tools, discovery engine, and observability.
- [**Roadmap**](ROADMAP.md)

## License

MIT.
