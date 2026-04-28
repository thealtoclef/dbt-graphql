# System Architecture

How dbt-graphql solves the "second modeling layer" problem — from dbt artifacts to production API.

---

## Problem → Solution

**Problem:** Analytics teams maintain dbt projects with models, tests, constraints, and lineage. Exposing this data as an API requires either (a) live DB introspection (loses all semantic metadata), or (b) hand-building a second schema layer (duplication → drift).

**Solution:** Read dbt artifacts (`catalog.json` + `manifest.json`) through a 3-phase compiler pipeline. The dbt project is the single source of truth — no duplication, no live introspection.

```
dbt artifacts          IR boundary              Consumers
────────────────────────────────────────────────────────────
catalog.json    ──→  ProjectInfo (IR)  ──→  GraphQL API (/graphql)
manifest.json         Pydantic models         MCP Server  (/mcp)
                                              Compiler (SQL)
                                              Cache + Auth + OTel
```

The IR boundary is the most important architectural decision: everything upstream produces `ProjectInfo`, everything downstream consumes it. No downstream module reads dbt artifacts directly.

---

## Data flow

```
dbt artifacts (catalog.json, manifest.json)
      │
      ▼
extract_project() — pipeline.py
  ├── artifacts.py (load & validate)
  ├── processors/ (3-way relationship merge)
  │   ├── constraints.py    (dbt v1.5+ PK/FK contracts)     ┐
  │   ├── data_tests.py     (relationships + enum tests)     ├── independent
  │   └── compiled_sql.py   (sqlglot lineage + JOIN mining)  ┘
  └── ir/models.py (ProjectInfo IR)
      │
      ▼
build_registry() — formatter/graphql.py
  └── TableRegistry (Python schema representation)
      │
      ├─── [generate mode] ──→ db.graphql + lineage.json
      │
      └─── [serve mode]
              │
              ├── GraphQL API (/graphql)
              │     resolvers → compile_query → DatabaseManager → warehouse
              │
              ├── MCP Server (/mcp) — 7 discovery + query tools
              │     run_graphql → same engine, same policies
              │
              ├── Auth: JWT verification → PolicyEngine
              │     column allow/deny, masks, row filters
              │
              ├── Cache: result cache + singleflight
              │     TTL-based, cashews backend, Redis for multi-replica
              │
              └── OTel: traces + metrics + logs
```

---

## Pipeline phases

### Phase 1 — Extraction

**Entry point:** `pipeline.extract_project()`

1. **Load** — `artifacts.py` reads and validates `catalog.json` and `manifest.json` using `dbt-artifacts-parser` (supports dbt schema versions v1–v12).
2. **Process** — Three independent processors run:
   - `constraints.py` — Extracts PK/FK relationships from dbt v1.5+ contract constraints
   - `data_tests.py` — Detects enums from `accepted_values` tests and FKs from `relationships` tests
   - `compiled_sql.py` — Extracts column lineage and discovers FK relationships from compiled SQL via sqlglot AST analysis
3. **Merge** — Relationships merged with priority ordering: `constraints > data_tests > compiled_sql`. Deduplication key: `{from_model}_{from_col}_{to_model}_{to_col}`.
4. **Emit** — Produces `ProjectInfo` (Pydantic models): the format-agnostic IR.

### Phase 2 — Formatting

**Entry point:** `formatter/graphql.py`

- `build_registry()` converts `ProjectInfo` → `TableRegistry` (Python schema objects)
- In generate mode: `format_graphql()` serializes to SDL → `db.graphql`; `build_lineage_schema()` → `lineage.json`
- `parse_db_graphql()` enables round-trip: SDL → `TableRegistry` (useful for CI artifacts shipped without dbt)

### Phase 3 — Serving

**Entry point:** `serve/create_app()`

Two modes, one binary:

| Mode         | Command                                          | DB required | Output                               |
| ------------ | ------------------------------------------------ | ----------- | ------------------------------------ |
| **Generate** | `dbt-graphql --config config.yml --output ./out` | No          | `db.graphql` + `lineage.json`        |
| **Serve**    | `dbt-graphql --config config.yml`                | Yes         | HTTP server (GraphQL + optional MCP) |

The Starlette application mounts two transports:

#### GraphQL API (`/graphql`)

- Ariadne async execution with per-table resolvers
- `compile_query()` translates GraphQL selections into correlated subqueries
- `DatabaseManager` manages async SQLAlchemy connection pools
- Query guards enforce depth, field count, and limit caps

#### MCP Server (`/mcp`, opt-in)

- FastMCP with Streamable HTTP transport
- 7 tools for schema discovery, join-path search, and query execution
- Shares the same `TableRegistry`, executable schema, policies, and connection pool as GraphQL
- `run_graphql` tool routes through the same engine — column allow-lists, masks, and row filters apply uniformly

---

## Component descriptions

### Authentication (JWT)

Follows the OAuth 2.0 Resource Server model. An external identity provider (Auth0, Keycloak, Cognito, etc.) issues signed JWTs; dbt-graphql verifies the signature, reads the payload, and evaluates policy against it. Supports JWKS rotating key sets (httpx async with monotonic TTL) and static key sources (env var, file, URL). See [security.md](security.md).

### Policy Engine

Evaluates RBAC policies declared inline in `config.yml`. Each policy has a `when` condition (simpleeval expression against JWT claims) and per-table rules for column access, data masking, and row filters. Supports both `allow` and `deny` effects with deny-wins precedence. Default-deny at the table level; strict-column enforcement (unauthorized columns produce errors, not silent stripping). See [access-policy.md](access-policy.md).

### Compiler

Translates GraphQL selection sets into warehouse SQL using correlated subqueries (not LATERAL joins, for Apache Doris compatibility). Dialect-aware JSON aggregation via SQLAlchemy `@compiles` extensions (JSONB_AGG for PostgreSQL, JSON_ARRAYAGG for MySQL/MariaDB/Doris). Policy enforcement (column stripping, mask injection, row filter WHERE clauses) happens at compile time — unfiltered data never leaves the database engine. See [compiler.md](compiler.md).

### Cache

Result cache + singleflight via cashews. Keyed by SHA-256 of rendered SQL + bound parameters, ensuring cross-tenant isolation. In-memory LRU by default; Redis for multi-replica deployments. Singleflight coalesces identical concurrent requests into a single warehouse roundtrip. See [caching.md](caching.md).

### Pool Admission Control

The SQLAlchemy connection pool acts as the admission queue. Configurable pool size, max overflow, timeout, and recycle. On checkout timeout, the resolver returns a structured `POOL_TIMEOUT` error that the HTTP handler elevates to HTTP 503 + `Retry-After`.

### Observability

Full OpenTelemetry instrumentation: traces, metrics, and logs. Auto-instrumented for SQLAlchemy, Starlette, and httpx. Custom metrics for cache outcomes, auth results, and connection wait times.

---

## Key invariants

| Invariant                    | Description                                                                                                                      |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **dbt-as-source-of-truth**   | The dbt project defines the schema. dbt-graphql cannot invent metadata the dbt project doesn't have.                             |
| **Format-agnostic IR**       | `ProjectInfo` is the single boundary between extraction and all consumers. No downstream module reads dbt artifacts directly.    |
| **Read-only SQL**            | Every compiled query is a `SELECT` tree. No mutations, writes, or DDL.                                                           |
| **Compile-time enforcement** | Row filters, column stripping, and mask injection happen during SQL compilation — they cannot be bypassed at runtime.            |
| **Deny-wins policy merge**   | Any matching deny rule takes precedence over all matching allow rules. Cross-cutting prohibitions are always enforced.           |
| **Default-deny columns**     | Tables not covered by a matching allow policy produce `FORBIDDEN_TABLE`. Unauthorized columns produce `FORBIDDEN_COLUMN` errors. |
| **Correlated subqueries**    | Nested relations compile to correlated subqueries (no LATERAL), ensuring compatibility with Apache Doris and older engines.      |

---

## Database support

| Database         | Driver   | Notes                                                      |
| ---------------- | -------- | ---------------------------------------------------------- |
| **PostgreSQL**   | asyncpg  | JSON aggregation via `JSONB_AGG`                           |
| **MySQL**        | aiomysql | JSON aggregation via `JSON_ARRAYAGG`                       |
| **MariaDB**      | aiomysql | Same driver as MySQL; compatible SQL dialect               |
| **Apache Doris** | aiomysql | No LATERAL support — correlated subqueries used throughout |

---

## Detailed documentation

| Document                                    | Content                                                           |
| ------------------------------------------- | ----------------------------------------------------------------- |
| [Architecture & Design](architecture.md)    | Design principles, pipeline flow, landscape comparison, prior art |
| [Schema Synthesis](schema-synthesis.md)     | dbt extraction, IR design, formatter, and lineage in depth        |
| [GraphQL API](graphql.md)                   | ASGI sub-app, resolvers, auth, error handling                     |
| [Compiler](compiler.md)                     | GraphQL → SQL compilation with correlated subqueries              |
| [Caching & Burst Protection](caching.md)    | Result cache + singleflight design and configuration              |
| [Access Policy](access-policy.md)           | RBAC, row filters, column masking, policy DSL reference           |
| [Security](security.md)                     | JWT verification, threat model, anonymous mode                    |
| [Configuration Reference](configuration.md) | Full config surface with env-var precedence                       |
| [MCP Server](mcp.md)                        | Tools, discovery engine, and observability                        |
