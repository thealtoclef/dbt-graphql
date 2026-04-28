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
