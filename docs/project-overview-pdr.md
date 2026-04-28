# Project Overview — Product Development Requirements

## Project identity

| Field          | Value                                                                                                       |
| -------------- | ----------------------------------------------------------------------------------------------------------- |
| **Name**       | dbt-graphql                                                                                                 |
| **Version**    | 0.1.0                                                                                                       |
| **License**    | MIT (Copyright 2026 The Alto Clef)                                                                          |
| **Tagline**    | Turn a dbt project into a typed GraphQL schema, a SQL-backed GraphQL API, and an MCP surface for LLM agents |
| **Repository** | [github.com/minhluc-info/dbt-graphql](https://github.com/minhluc-info/dbt-graphql)                          |

---

## The problem

Analytics teams maintain rich dbt projects — models, tests, constraints, descriptions, column lineage — but exposing that data as an API requires building and maintaining a **second modeling layer**. The typical approach is to introspect the live database with tools like Hasura or PostGraphile, which throws away everything the dbt project knows: descriptions, test-encoded semantics, modeled relationships, and lineage.

Three specific pain points:

| Pain point          | Details                                                                                                                                                                                                          |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Duplication**     | Teams define models, columns, relationships, and tests in dbt. To create a GraphQL API, they must re-define the schema in another tool (Hasura, PostGraphile, hand-rolled). Two sources of truth → schema drift. |
| **Lost semantics**  | Live DB introspection only sees raw tables/views. Descriptions, test-encoded constraints, relationship metadata, and lineage that the dbt team maintains are invisible.                                          |
| **No agent access** | LLM agents need to understand the warehouse to query data, but have no structured way to discover schema, relationships, or join paths without writing raw SQL.                                                  |

---

## The solution

dbt-graphql reads two files your dbt project already produces — `catalog.json` and `manifest.json` — and **derives everything** from them. No second modeling layer, no live database introspection, no hand-written resolvers.

### How it works

```
dbt artifacts (catalog.json, manifest.json)
        │
        ▼
  extract_project() — pipeline.py
    ├── 3 independent processors (constraints, data tests, compiled SQL)
    ├── 3-way relationship merge with priority ordering
    └── ProjectInfo (format-agnostic IR)
        │
        ▼
  build_registry() — formatter/graphql.py
    └── TableRegistry (Python schema representation)
        │
        ├── [generate mode] ──→ db.graphql + lineage.json
        │
        └── [serve mode]
                ├── GraphQL API (/graphql) — resolvers → SQL → warehouse
                ├── MCP Server (/mcp) — 7 tools for LLM agents
                ├── JWT Auth + RBAC (column/row/mask at SQL compile time)
                ├── Result cache + singleflight
                └── OpenTelemetry (traces + metrics + logs)
```

### Four core guarantees

| Guarantee                     | How                                                                                                                                                                                                                                                      |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Zero duplication**          | Schema, types, relationships, enums — all derived from dbt artifacts. The dbt project is the single source of truth.                                                                                                                                     |
| **Read-only safe**            | Compiles exclusively to `SELECT` statements. No mutations, no writes, no DDL.                                                                                                                                                                            |
| **Production-grade security** | JWT auth (OAuth 2.0 Resource Server), RBAC with column-level security, Hasura-style row filters, and data masking — all evaluated at SQL compile time. Row filter values become bound SQL parameters, making cross-tenant leaks structurally impossible. |
| **MCP-first for agents**      | 7 MCP tools allow LLM agents to discover schema, find join paths, trace column lineage, and execute queries — all through the same auth/policy engine as the GraphQL API.                                                                                |

### 3-way relationship merge

dbt-graphql derives table relationships from three independent sources, merged with priority ordering:

1. **dbt constraints** (highest) — PK/FK from v1.5+ `contract.enforced` declarations
2. **dbt data tests** — `relationships` tests for FK, `accepted_values` for enums
3. **Compiled SQL analysis** (lowest) — sqlglot JOIN-ON mining + column lineage via dbt-colibri

This means **any dbt project — from basic to mature — gets a working schema with relationships automatically**.

---

## Target users

| User                    | How they use dbt-graphql                                           |
| ----------------------- | ------------------------------------------------------------------ |
| **Data engineers**      | Expose dbt models as a typed GraphQL API without writing resolvers |
| **Analytics engineers** | Author queries through GraphQL that compile to warehouse SQL       |
| **Platform teams**      | Secure the API with JWT auth, RBAC, row filters, and data masking  |
| **LLM / AI agents**     | Discover schema and query data via MCP tools                       |

---

## Feature list (shipped)

### dbt artifact integration

- dbt artifact parsing (`manifest.json` + `catalog.json`, dbt schema versions v1–v12)
- SQL→GraphQL type mapping with automatic enum detection (`accepted_values` tests)
- Relationship derivation from three independent sources (priority-ordered):
  1. dbt constraints (`primary_key` / `foreign_key`, v1.5+)
  2. dbt data tests (`relationships` tests)
  3. Compiled SQL lineage analysis (sqlglot + dbt-colibri)
- Column-level lineage extraction via sqlglot's AST traversal

### GraphQL API

- Async GraphQL server built on Ariadne + Starlette
- Per-table resolvers that compile GraphQL to correlated subqueries
- Nested relation support without N+1 queries
- Query guard rails (depth, field count, limit caps)
- GraphQL introspection (configurable, off by default in production)

### Multi-database support

- PostgreSQL (asyncpg)
- MySQL (aiomysql)
- MariaDB
- Apache Doris

### Authentication & authorization

- JWT authentication following the OAuth 2.0 Resource Server model
- JWKS rotating key set support (httpx async + monotonic TTL)
- Static key sources (env var, file, URL) as alternatives
- RBAC with allow/deny effects (XACML / AWS IAM convention)
- Column-level security (include_all, includes, excludes)
- Row-level security via Hasura-style structured filter DSL
- Data masking with SQL expressions
- Deny-wins policy merge — cross-cutting prohibitions always take precedence

### Caching & protection

- Result cache with configurable TTL (cashews backend)
- Singleflight coalescing for identical concurrent queries
- Redis support for multi-replica cache sharing
- Pool admission control (HTTP 503 + Retry-After on timeout)

### MCP server

- 7 tools: `list_tables`, `describe_table`, `find_path`, `explore_relationships`, `trace_column_lineage`, `build_query`, `run_graphql`
- Streamable HTTP transport via FastMCP
- Schema discovery with BFS join-path finding
- Live enrichment (row counts, sample rows, value summaries)
- Shares the same auth, policies, and connection pool as the GraphQL API

### Observability

- Full OpenTelemetry instrumentation (traces, metrics, logs)
- OTel exporters for all three signals (OTLP HTTP/gRPC)
- Auto-instrumentation for SQLAlchemy, Starlette, and httpx

### CLI

- **Generate mode** — Output `db.graphql` (SDL) + `lineage.json` without a server
- **Serve mode** — Run the GraphQL (and optionally MCP) server via uvicorn
- Flat CLI: `--config` + `--output` (no subcommands)

---

## Planned features

### P0 — Next up

- **Relay-style cursor pagination** — Connection types with `first`/`after`/`last`/`before`
- **Query Allow-List** — Lock the API to known query shapes in production
- **Audit logging** — Per-request structured logs for compliance and forensics
- **MCP SOTA Surface** — Additional tools, resources, and prompt templates

### P1 — Soon

- **Few-shot Q→GraphQL example store** — Lexical retriever for example queries
- **dbt Selector support** (`--select`) — Use `dbt ls` to filter models
- **Source node inclusion** — Expose `catalog.sources` as read-only tables
- **Hot reload of access policies** — Watch config and swap PolicyEngine atomically
- **Policy test harness** — `policy explain` CLI + inline test blocks

### P2 — Later

- **Column classifications** — Reusable PII/sensitivity classes with mask templates

### P3 — Placeholder

- **Python extension hooks** — Superset-style overrides file for custom resolvers, masks, and audit sinks

---

## Architecture summary

dbt-graphql follows a three-phase pipeline:

1. **Extraction** — Load dbt artifacts and run three independent processors (constraints, data tests, compiled SQL lineage) to produce a unified `ProjectInfo` intermediate representation.
2. **Formatting** — Convert the IR into a `TableRegistry` (Python schema) and optionally emit GraphQL SDL + lineage JSON.
3. **Serving** — Compile GraphQL queries to warehouse SQL via correlated subqueries, enforce access policies at compile time, and serve results through GraphQL and MCP transports.

For the full architecture and design rationale, see [system-architecture.md](system-architecture.md) and [architecture.md](architecture.md).

---

## Related documentation

- [System Architecture](system-architecture.md) — 3-phase pipeline, data flow, key invariants
- [Architecture & Design](architecture.md) — design principles, competitive landscape
- [Codebase Summary](codebase-summary.md) — source layout and module descriptions
- [Design Guidelines](design-guidelines.md) — IR boundary, processor pattern, security principles
- [Configuration Reference](configuration.md) — full config surface with env-var precedence
- [Access Policy](access-policy.md) — RBAC, row filters, column masking
- [Security](security.md) — JWT verification, threat model
- [Roadmap](project-roadmap.md) — shipped features, planned work, follow-ups
- [Deployment Guide](deployment-guide.md) — production setup, Docker, reverse proxy
