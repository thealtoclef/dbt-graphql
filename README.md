# dbt-graphql

Turn a dbt project into a typed GraphQL schema, a SQL-backed GraphQL API, and an MCP surface for LLM agents — without authoring a second modeling layer.

---

## The problem

Analytics teams maintain rich dbt projects — models, tests, constraints, descriptions, column lineage — but exposing that data as an API requires building a **second modeling layer**. Tools like Hasura or PostGraphile introspect the live database, which throws away everything the dbt project already knows: descriptions, test-encoded semantics, modeled relationships, and lineage.

Meanwhile, LLM agents that need to query the warehouse have no structured way to discover schema, relationships, or join paths — they're left writing raw SQL by guesswork.

## The solution

dbt-graphql reads two files your dbt project already produces — `catalog.json` and `manifest.json` — and **derives everything** from them:

```
dbt artifacts  →  extract_project()  →  ProjectInfo (IR)  →  GraphQL schema + API + MCP tools
```

No second modeling layer. No live database introspection. No hand-written resolvers.

**Four core guarantees:**

| Guarantee                     | How                                                                                    |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| **Zero duplication**          | Schema, types, relationships, enums — all derived from dbt artifacts                   |
| **Read-only safe**            | Compiles exclusively to `SELECT` statements; no mutations, no writes                   |
| **Production-grade security** | JWT auth + RBAC (column/row/mask) evaluated at SQL compile time, not application layer |
| **MCP-first for agents**      | 7 MCP tools for schema discovery, join-path finding, and query execution               |

**How it works — the 3-way relationship merge:**

dbt-graphql derives table relationships from three independent sources, merged with priority ordering:

1. **dbt constraints** (highest) — PK/FK from v1.5+ contract enforcement
2. **dbt data tests** — `relationships` tests, `accepted_values` → enums
3. **Compiled SQL analysis** (lowest) — sqlglot JOIN-ON mining + column lineage via dbt-colibri

This means any dbt project — from basic to mature — gets a working schema with relationships automatically.

---

## Features

- **Multi-database** — PostgreSQL, MySQL, MariaDB, Apache Doris
- **GraphQL API** — async resolvers compile to correlated subqueries (no N+1)
- **MCP server** — 7 tools for LLM agents: `list_tables`, `describe_table`, `find_path`, `explore_relationships`, `trace_column_lineage`, `build_query`, `run_graphql`
- **JWT auth + RBAC** — OAuth 2.0 Resource Server model, column-level security, Hasura-style row filters, data masking
- **Caching** — result cache + singleflight (Redis for multi-replica)
- **Observability** — full OpenTelemetry instrumentation (traces, metrics, logs)
- **Two modes** — `generate` (output `db.graphql` + `lineage.json`) and `serve` (uvicorn HTTP server)

---

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
  mcp_enabled: false # opt-in; expose MCP tools to LLM agents
  graphql_introspection: false # off in prod; on for dev tooling
```

## Query layer

Each table exposes one root field that returns a `{T}Result` envelope —
paginated rows, inline aggregates, and Cube-style GROUP BY share the same
filter and the same DB round-trip when their fields are siblings.

```graphql
{
  orders(where: {
    _and: [
      { status: { _in: ["completed", "shipped"] } },
      { _or: [{ amount: { _gte: 100 } }, { vip: { _eq: true } }] }
    ]
  }) {
    nodes(order_by: [{ amount: desc }], limit: 50) {
      order_id amount status
      customer { customer_id name }   # nested via correlated subquery (no LATERAL)
    }
    count
    sum_amount
    avg_amount
    group(order_by: [{ count: desc }]) {
      status
      count
      sum_amount
    }
  }
}
```

- **`{T}_bool_exp`** (Hasura vocab): `_and` / `_or` / `_not` plus per-column
  `_eq` / `_neq` / `_gt` / `_gte` / `_lt` / `_lte` / `_in` / `_nin` / `_is_null`
  / `_like` / `_nlike` / `_ilike` / `_nilike`. The same operator set is reused
  by access-policy `row_filter` blocks.
- **`{T}_order_by`** (and `{T}_group_order_by`): flat `column: asc | desc`.
- **Aggregates**: `count` always; `sum_<col>` / `avg_<col>` / `stddev_<col>` /
  `var_<col>` for numeric columns; `min_<col>` / `max_<col>` for every scalar
  column. Selecting any combination fires one batched SELECT per request.
- **`group`**: GROUP BY columns are auto-derived from whichever real-column
  fields you select on `{T}_group` — no separate root entry, no nested
  `aggregate { sum { col } }`.

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
      effect: allow # IAM-style; required, no default
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

---

## Documentation

### Start here

- [**Project Overview**](docs/project-overview-pdr.md) — problem, solution, target users, shipped & planned features
- [**System Architecture**](docs/system-architecture.md) — 3-phase pipeline, data flow, key invariants

### Operational guides

- [**Deployment Guide**](docs/deployment-guide.md) — production checklist, Docker, reverse proxy, env vars
- [**Configuration Reference**](docs/configuration.md) — full config surface and env-var precedence
- [**Access Policy**](docs/access-policy.md) — RBAC, structured row filters, column masking
- [**Security**](docs/security.md) — JWT verification, threat model, anonymous mode

### Technical deep-dives

- [**Architecture & Design**](docs/architecture.md) — design principles, pipeline flow, competitive landscape
- [**Schema Synthesis**](docs/schema-synthesis.md) — dbt extraction, IR, formatter, and lineage in depth
- [**GraphQL API**](docs/graphql.md) — ASGI sub-app, resolvers, auth, observability
- [**Compiler**](docs/compiler.md) — GraphQL → SQL with correlated subqueries
- [**Caching & Burst Protection**](docs/caching.md) — result cache + singleflight design
- [**MCP Server**](docs/mcp.md) — tools, discovery engine, and observability

### Contributing

- [**Codebase Summary**](docs/codebase-summary.md) — source layout and module descriptions
- [**Code Standards**](docs/code-standards.md) — language, tooling, and naming conventions
- [**Design Guidelines**](docs/design-guidelines.md) — architectural principles and patterns
- [**Project Roadmap**](docs/project-roadmap.md) — shipped features, planned work, follow-ups

---

## License

MIT.
