# dbt-graphql

Turn a dbt project into a typed GraphQL schema, a SQL-backed GraphQL API, and an MCP surface for LLM agents — without authoring a second modeling layer. dbt-graphql reads `catalog.json` and `manifest.json` and derives everything from what your analytics team already maintains.

## Installation

```bash
pip install dbt-graphql                    # core (generate + serve)
pip install dbt-graphql[postgres]          # + asyncpg
pip install dbt-graphql[mysql]             # + aiomysql
pip install dbt-graphql[redis]             # + Redis-backed cache for multi-replica
```

## Quick start

The CLI takes a single `--config` flag pointing at `config.yml`. See
[`config.example.yml`](config.example.yml) for a documented template.

**1. Generate schema files (no DB connection required)**

```bash
dbt-graphql --config config.yml --output ./out
# → out/db.graphql, out/lineage.json
```

**2. Serve the API**

```bash
dbt-graphql --config config.yml
```

GraphQL is always mounted at `/graphql` in serve mode. Set
`serve.mcp_enabled: true` in `config.yml` to additionally co-mount the
MCP server at `/mcp`. Both transports share one Granian process, one
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

## Access policy

Per-request RBAC, row filters (Hasura-style structured DSL), and
column masking — all evaluated at SQL compile time:

```yaml
# access.yml
policies:
  - name: analyst
    when: "'analysts' in jwt.groups"
    tables:
      customers:
        column_level:
          include_all: true
          mask:
            email: "CONCAT('***@', SPLIT_PART(email, '@', 2))"
        row_filter:
          org_id: { _eq: { jwt: claims.org_id } }
```

See [`access.example.yml`](access.example.yml) and
[docs/access-policy.md](docs/access-policy.md).

## Documentation

- [**Architecture & Design**](docs/architecture.md) — pipeline, design principles, and landscape comparison.
- [**Schema Synthesis**](docs/schema-synthesis.md) — dbt extraction, IR, formatter, and lineage in depth.
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
