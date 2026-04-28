# Architecture & Design

The design doc for dbt-graphql: the problem it solves, how the pipeline fits together, and the design principles that govern every component.

**Component deep-dives:** [Schema Synthesis](schema-synthesis.md) | [Compiler](compiler.md) | [GraphQL HTTP](graphql.md) | [Caching & Burst Protection](caching.md) | [MCP Server](mcp.md) | [Security](security.md) | [Access Policy](access-policy.md)

---

## Table of contents

- [1. The problem and the shape of the solution](#1-the-problem-and-the-shape-of-the-solution)
- [2. Pipeline flow](#2-pipeline-flow)
- [3. Design principles](#3-design-principles)
- [4. Intermediate representation](#4-intermediate-representation)
- [5. CLI](#5-cli)
- [6. Cross-cutting design notes](#6-cross-cutting-design-notes)
- [7. Landscape & decisions](#7-landscape--decisions)

---

## 1. The problem and the shape of the solution

A dbt project already contains the things an analytics API needs:

- a typed model graph (`manifest.json`: models, columns, descriptions, tests, constraints, refs)
- resolved warehouse types per column (`catalog.json`)
- test-encoded semantics: `relationships` tests behave as foreign keys, `unique` and `not_null` tests as column constraints, `accepted_values` as enums
- dbt v1.5+ first-class `primary_key` / `foreign_key` constraints
- a dependency graph (table-level lineage for free; column-level via sqlglot)

The usual response to "give my warehouse a GraphQL API" is to introspect the live database (Hasura, PostGraphile, pg_graphql, GraphJin). That throws away everything the dbt project knows — descriptions, tests, modeled relationships, lineage — and re-derives a shallower schema from raw DDL.

dbt-graphql takes the other route: **the dbt project is the source of truth**. Its job is to:

1. Project dbt artifacts into a neutral intermediate representation (IR).
2. Emit a typed GraphQL SDL that preserves dbt semantics via custom directives.
3. Compile GraphQL queries against that SDL into warehouse SQL.
4. Expose the same SDL and an operational surface to LLM agents via MCP.

Steps 2–4 never touch `manifest.json` again. The only coupling to dbt lives in step 1.

---

## 2. Pipeline flow

Primary pipeline (both modes):

```
  catalog.json  ─┐
  manifest.json  ─┘──▶ extract_project() ──▶ ProjectInfo ──▶ build_registry() ──▶ TableRegistry
                             │                                                           │
                             ▼                                               ┌───────────┴──────────┐
                      dbt/processors/                                        ▼                      ▼
                        artifacts.py       — load manifest & catalog    GraphQL API            MCP tools
                        constraints.py     — dbt v1.5+ constraints        (graphql/)             (mcp/)
                        data_tests.py      — not_null / unique / enums / FKs
                        compiled_sql.py    — sqlglot → lineage + JOIN FKs
```

Side export (`--output DIR` only — no server is started):

```
  TableRegistry  ──▶  _registry_to_sdl()        ──▶  db.graphql
  ProjectInfo    ──▶  build_lineage_schema()     ──▶  lineage.json
```

Entry point: [`src/dbt_graphql/pipeline.py`](../src/dbt_graphql/pipeline.py) — `extract_project()`.

The extraction + SDL-emission phase (the dbt processors, IR, formatter, and lineage builder) is called the **schema synthesis** phase. See [schema-synthesis.md](schema-synthesis.md) for the full step-by-step.

---

## 3. Design principles

A few opinions shaped the code. When in doubt, these are the tiebreakers.

### 3.1 The dbt project is the source of truth

We do not ask users to author a second modeling layer. If dbt has a `relationships` test, it is an edge in the graph. If dbt has a `unique` test, it is a `@unique` directive. If dbt has `primary_key` constraints, they are emitted as the built-in `ID` scalar. The cost of adopting dbt-graphql is approximately zero beyond "keep your dbt project clean."

Consequence: dbt-graphql **cannot** invent metadata the dbt project doesn't have. Missing descriptions stay missing. Missing relationships stay missing. The tool's correctness is bounded by the quality of your dbt project.

### 3.2 A single format-agnostic IR

`ProjectInfo` is the boundary between dbt and formatters. Every formatter, compiler, and MCP tool consumes `ProjectInfo` — never `manifest.json` directly. This is what makes it tractable to add alternative output formats (OpenAPI, JSON Schema, Malloy, …) or swap the upstream source without touching the rest of the code.

### 3.3 Dataclasses for processors, Pydantic for IR

- `dbt/processors/*` produce lightweight `@dataclass` types — they're internal, mutable, and fast.
- `ir/models.py` uses `BaseModel` — it's the long-lived contract, needs validation, serialization, and aliasing (`schema_` ↔ `schema`).

Crossing the boundary is a deliberate step: `pipeline.extract_project()` converts processor types into Pydantic IR types.

### 3.4 Preserve the SQL type

GraphQL type systems are coarse. SQL type systems are precise (`NUMERIC(10,2)` matters). We pick both: the GraphQL field gets a standard scalar (`Int`, `Float`, `Boolean`, `String`) for tooling compatibility, and an `@column(type: "NUMERIC", size: "10,2")` directive preserves the exact database type for the compiler. The compiler never has to reverse-engineer SQL types from GraphQL scalar names.

### 3.5 Read-only by design

No mutations, no writes, no upserts. The target is always a `SELECT` tree. That removes an entire class of write-path risk and simplifies the compiler. If you need a write-heavy GraphQL backend, use Hasura or PostGraphile.

### 3.6 MCP-first for agents

The primary consumer of dbt-graphql is **an agent**, not a human writing GraphQL in a playground. The MCP server is not a port of the HTTP API — it's a distinct surface designed around how agents actually plan: first list, then describe, then find a join path, then build a query, then execute. Each MCP tool returns a `_meta.next_steps` field guiding the next call.

### 3.7 Cross-warehouse, not Postgres-only

SQL is emitted via SQLAlchemy Core so we get dialect-aware rendering for free. Where SQLAlchemy doesn't cover it (JSON aggregation varies wildly per engine), we use the `@compiles` extension to register per-dialect rewrites. No LATERAL joins (Apache Doris doesn't support them).

### 3.8 Don't parse what dbt already parsed

`dbt_artifacts_parser` owns manifest/catalog schema validation. `sqlglot` owns SQL parsing/qualification for column and join lineage. `sqlalchemy` owns dialect SQL generation. `graphql-core` owns GraphQL parsing. `ariadne` owns execution. dbt-graphql is the glue — small, opinionated, replaceable.

---

## 4. Intermediate representation

[`src/dbt_graphql/ir/models.py`](../src/dbt_graphql/ir/models.py)

`ProjectInfo` is the single boundary between extraction and everything that follows. Four Pydantic models — `ColumnInfo`, `ModelInfo`, `RelationshipInfo`, `ProjectInfo` — carry the full dbt semantics into formatters, compilers, and the MCP layer. None of these ever look at `manifest.json` directly.

See [schema-synthesis.md § 2](schema-synthesis.md#2-intermediate-representation-irmodelspy) for the full field-level design notes.

---

## 5. CLI

[`src/dbt_graphql/cli.py`](../src/dbt_graphql/cli.py)

One entry point, two modes controlled by `--output`:

```
dbt-graphql --config config.yml                # serve mode
dbt-graphql --config config.yml --output DIR   # export mode
```

**Serve mode** (no `--output`):
1. `load_config()` reads `config.yml`; dbt artifact paths resolved relative to the config file.
2. `extract_project()` builds `ProjectInfo` from dbt artifacts.
3. `build_registry()` builds `TableRegistry` from `ProjectInfo` — the Python object consumed by the serve layer.
4. Mount transports under one uvicorn process:
   - GraphQL at `/graphql` (always).
   - MCP at `/mcp` when `serve.mcp_enabled: true`. The MCP server shares the GraphQL bundle: the `TableRegistry` drives schema discovery, the executable schema backs `run_graphql`, and the same `PolicyEngine` filters every tool's view. dbt artifacts contribute descriptions and enum metadata only.

**Export mode** (`--output DIR`):
Steps 1–2 only, then `_registry_to_sdl()` writes `db.graphql` and `build_lineage_schema()` writes `lineage.json` to `DIR`. No server is started.

---

## 6. Cross-cutting design notes

1. **Directives encode metadata the SDL readers need.** `@column`, `@unique`, `@relation`, `@table` — together they make `db.graphql` self-sufficient at runtime.
2. **Correlated subqueries over LATERAL.** Portable to Apache Doris and older engines. See [compiler.md § 2](compiler.md#2-why-correlated-subqueries-not-lateral-joins).
3. **Standard scalars + `@column`.** Familiar GraphQL types, exact warehouse types preserved in directives. See principle 3.4.
4. **Next-steps hint pattern.** Each MCP tool response includes `_meta.next_steps`. See [mcp.md](mcp.md).
5. **Enum deduplication by sorted value set.** `accepted_values(['a','b'])` on two columns becomes one enum, not two.
6. **Read-only.** See principle 3.5. If this ever changes, it's a major version.
7. **Bidirectional relationship adjacency for BFS.** Every `RelationshipInfo` is stored on both the from-model and to-model; BFS works in either direction.
8. **Database config decoupled from dbt profiles.** See [compiler.md § 5](compiler.md#5-connection-management-compilerconnectionpy).
9. **Relationship origin tiers.** Every `RelationshipInfo` records its source (`constraint` > `data_test` > `lineage`). Constraints and tests are user-authored; lineage-inferred relationships are best-effort.
10. **Processor modules are source-based, not output-based.** `constraints.py` / `data_tests.py` / `compiled_sql.py` each correspond to one input surface. When chasing a bug, "where did this fact come from?" is the question that matters.
11. **Column lineage via dbt-colibri's traversal approach.** The core recursive `to_node()` logic (qualify → build_scope → CTE/subquery/UNION/PIVOT resolution, max-rank classification) is absorbed into `compiled_sql.py` — no runtime dependency on dbt-colibri. See [schema-synthesis.md § 4](schema-synthesis.md#4-lineage-extraction).
12. **Result cache + singleflight between HTTP and the warehouse.** Keyed by rendered SQL + bound parameters, so cross-tenant isolation is structural. Wall-clock TTLs only — no refresh-key probing. The lock coalesces concurrent identical requests into a single warehouse roundtrip. See [caching.md](caching.md).

---

## 7. Landscape & decisions

Where dbt-graphql sits relative to adjacent tools, the prior art it draws from, and an honest assessment of what it does well and what's missing.

> dbt-graphql occupies the intersection of two mature spaces — GraphQL-over-SQL (Hasura, PostGraphile, pg_graphql) and AI semantic layers (Cube, MetricFlow, Wren, Malloy) — and picks the smallest viable middle: no new modeling language, no mutations, no separate metrics store. The bet is that for agent-driven analytics, a typed, lineage-aware, read-only GraphQL schema derived straight from dbt is the fastest path to correct answers.

### Prior art & direct inspirations

#### GraphJin ([dosco/graphjin](https://github.com/dosco/graphjin))

From GraphJin we take:

- The **single-query compilation model** — don't resolve relations with N+1 queries; emit JSON-aggregated correlated subqueries so nested GraphQL selections become one SQL statement.
- The **FK-driven relationship inference** — foreign keys (in our case, dbt `relationships` tests + constraints) define navigable edges in GraphQL.
- The **MCP-as-agent-surface** model — GraphJin v3 added an `mcp` subcommand exposing tools like `list_tables`, `describe_table`, `execute_graphql`; dbt-graphql adopts the same shape.

Where we differ: GraphJin introspects the **live database**; dbt-graphql introspects **dbt artifacts**. GraphJin has mutations; dbt-graphql doesn't. GraphJin has role-based authorization in YAML; dbt-graphql has none yet (policy is deferred to whatever sits in front of the serve layer).

#### Wren Engine / WrenAI ([Canner/wren-engine](https://github.com/Canner/wren-engine))

From Wren we take:

- The thesis that **LLMs benefit from a typed, declarative model graph** rather than raw `information_schema`.
- The positioning of an **MCP server as the native agent interface** to that model graph.
- The **native dbt integration** pattern — Wren reads `manifest.json` and `catalog.json` and projects them into MDL; dbt-graphql does the analogous projection into GraphQL SDL.

Where we differ: Wren's interface to agents is "write SQL against MDL" (text-to-SQL); dbt-graphql's is "write GraphQL and we compile it." Wren is a federated engine across 15+ data sources; dbt-graphql targets the single warehouse your dbt project already models. Wren ships a full GenBI UI (WrenAI); dbt-graphql is a library + CLI.

#### Other influences

- **[`dbt-colibri`](https://github.com/Datatonic/dbt-colibri)** — sqlglot-based column lineage over dbt artifacts. Its `lineage.py` is a modified fork of sqlglot's own lineage module (MIT). We absorbed the core traversal logic directly.
- **[PostGraphile](https://postgraphile.org)** — the principle that a schema-driven GraphQL API is a *projection* of a richer upstream model, not a hand-authored artifact.

### Semantic layers for AI

| Project | Core idea | Source-of-truth model | AI / agent integration | Query language | DB support | License | Stack |
|---|---|---|---|---|---|---|---|
| **dbt-graphql** (this project) | Turn dbt artifacts into GraphQL + SQL compiler + MCP | dbt `manifest.json` + `catalog.json` | MCP server: schema discovery, join-path search, query build + execute | GraphQL (compiled to SQL via SQLAlchemy) | Any SQLAlchemy-supported warehouse | MIT | Python |
| **[Cube](https://github.com/cube-js/cube)** | Universal semantic layer for BI and AI | Cube data model (YAML/JS cubes, views, measures) | Cube MCP server over HTTPS with OAuth | SQL API, REST, GraphQL, MDX | Postgres, BigQuery, Snowflake, Redshift, Databricks, 20+ | Apache-2.0 (core) | Node.js + Rust |
| **[dbt Semantic Layer / MetricFlow](https://github.com/dbt-labs/metricflow)** | Governed metrics on top of dbt | MetricFlow YAML (semantic models, metrics) in the dbt project | JDBC + GraphQL Semantic Layer APIs; MCP connectors emerging | MetricFlow query spec (metrics, dimensions, filters) | Any dbt-supported warehouse | Apache-2.0 | Python |
| **[Wren Engine](https://github.com/Canner/wren-engine)** | Open context engine for AI agents, MDL-based | MDL (Model Definition Language) | Semantic engine for MCP clients; powers WrenAI GenBI agent | SQL (planned via Apache DataFusion from MDL) | 15+ sources | Apache-2.0 | Rust + DataFusion |
| **[Malloy](https://github.com/malloydata/malloy)** | A modern language for data relationships and transformations | Malloy source files | Publisher semantic model server; VS Code extension | Malloy (compiled to SQL) | BigQuery, Snowflake, Postgres, MySQL, Trino, DuckDB | MIT | TypeScript |
| **[Vanna AI](https://github.com/vanna-ai/vanna)** | RAG-powered text-to-SQL | Training data: DDL, docs, example Q/SQL pairs | Python library; Streamlit, Flask, Slack integrations | Natural language → SQL | PG, MySQL, Snowflake, BigQuery, Redshift, SQLite, DuckDB, ClickHouse | MIT | Python |
| **[LangChain SQLDatabaseToolkit](https://docs.langchain.com/oss/python/integrations/tools/sql_database)** | Agent toolkit that introspects a DB and calls an LLM to write SQL | Live DB introspection via SQLAlchemy | Tools for a LangChain agent | Natural language → SQL | Anything SQLAlchemy supports | MIT | Python |
| **[LlamaIndex NLSQLTableQueryEngine](https://developers.llamaindex.ai/python/framework-api-reference/query_engine/NL_SQL_table/)** | Query engine that turns NL into SQL | Live DB schema + optional table retriever | Part of a LlamaIndex workflow / agent | Natural language → SQL | Anything SQLAlchemy supports | MIT | Python |

**Positioning.** dbt-graphql is not a semantic layer. Cube, MetricFlow, Wren, and Malloy all require you to author a second modeling artifact. dbt-graphql instead *derives* its interface from what dbt already knows — and publishes that as a typed GraphQL schema plus an MCP surface. Compared to LangChain/LlamaIndex/Vanna agent-side text-to-SQL, dbt-graphql gives the agent a *structured* interface (a GraphQL schema and an MCP catalog of joinable entities) instead of raw tables + prompt-engineering.

### GraphQL-to-SQL in open source

| Project | Core idea | Schema source | DB support | Language | Maturity | License |
|---|---|---|---|---|---|---|
| **dbt-graphql** (this project) | Generate GraphQL SDL from dbt artifacts; compile queries to SQL; serve via MCP | dbt `manifest.json` + `catalog.json` | Any SQLAlchemy-supported warehouse | Python | Early | MIT |
| **[Hasura graphql-engine](https://github.com/hasura/graphql-engine)** | Instant realtime GraphQL over multiple databases with RBAC, subscriptions, event triggers | Live DB introspection + metadata | Postgres, MS SQL, BigQuery, MongoDB, ClickHouse | Haskell (v2) / Rust (v3) | Production, large ecosystem | Apache-2.0 core / EE commercial |
| **[PostGraphile](https://postgraphile.org)** | Low-effort, high-performance GraphQL API from a Postgres schema | Live Postgres introspection | Postgres only | TypeScript / Node.js | Production, mature | MIT |
| **[GraphJin](https://github.com/dosco/graphjin)** | "Automagical" GraphQL-to-SQL compiler, no-code / config-only | Auto-discovered DB schema + relationships | Postgres, MySQL, MongoDB, SQLite, Oracle, MSSQL | Go | Active, smaller community | Apache-2.0 |
| **[pg_graphql](https://github.com/supabase/pg_graphql)** (Supabase) | Postgres extension exposing GraphQL via a SQL function | Live Postgres introspection inside the DB | Postgres only | Rust (Postgres extension) | Production (powers Supabase GraphQL) | Apache-2.0 |
| **[Dgraph](https://github.com/dgraph-io/dgraph)** | Graph-native distributed database that speaks GraphQL | User-defined GraphQL SDL stored inside Dgraph | Dgraph's own storage | Go | Production | Apache-2.0 |
| **[AWS AppSync / Amplify](https://aws.amazon.com/appsync/)** | Managed serverless GraphQL with real-time + offline sync | Generated from DynamoDB key schema, or custom SDL with JS/VTL resolvers | DynamoDB, Aurora, OpenSearch, arbitrary data sources | Managed (AWS) | Production, enterprise | Proprietary |

**Positioning.** Every other project either introspects the live database (Hasura, PostGraphile, pg_graphql, GraphJin), is graph-native (Dgraph), or asks you to hand-write SDL + resolvers (AppSync). dbt-graphql is the only one that treats **dbt** as the authoritative schema source — which means relationship edges come from `relationships` tests, PKs from `constraints`, descriptions from dbt docs, and **lineage** is exposable as a first-class field (something no DB-introspection tool can do, because lineage doesn't exist in `pg_catalog`).

### Honest assessment

#### Strengths

| Strength | Why it matters |
|---|---|
| **dbt-native** — the dbt project *is* the schema | No second modeling layer to maintain. Docs, tests, and constraints are reused verbatim. |
| **GraphQL schema as a first-class artifact** | Emitted SDL is inspectable by humans and machines; strong typing gives agents a deterministic target. |
| **Lineage built in** | Upstream/downstream model lineage can be exposed alongside the schema — structurally impossible for DB-introspection tools. |
| **MCP-first** | The MCP server exposes discovery, relationship search, query construction, and execution as distinct tools — matching how agents actually plan. |
| **Read-only by design** | Removes an entire class of write-path risk and simplifies the compiler. |
| **Cross-warehouse via SQLAlchemy** | Dialect-portable SQL generation; a natural seam to add EXPLAIN / query-plan inspection later. |
| **Python + dbt's runtime** | Easy to extend, easy to embed in data teams that already speak Python. |
| **Correlated subqueries over LATERAL** | Works on engines without LATERAL (Apache Doris), no extra machinery for nested relations. |

#### Gaps & open work

| Gap | What it means |
|---|---|
| **No metrics / semantic layer** | No measures, no predefined aggregations. For governed metrics, pair with MetricFlow or Cube. |
| **Read-only** (also a strength) | No mutations, writes, or upserts. Wrong tool for app backends. |
| **Single-process Python serving** | The serve layer is async but still Python. Hasura/PostGraphile handle high-throughput concurrent agent workloads more easily. |
| **Compiler feature coverage** | No filter/order on nested fields; `where` supports only equality; no operators, no aggregates. |
| **Maturity** | Needs an integration-test corpus covering real dbt projects across dialects. |
| **GraphQL feature coverage** | No subscriptions, unions, federation, defer/stream. None are table stakes for analytics over a warehouse: subscriptions need an event/CDC source we don't own, federation belongs at a gateway in front of multiple sub-graphs (we are the sub-graph), and defer/stream is for chunked rendering not aggregation queries. |
| **No query-cost / safety guardrails** | No default `LIMIT`, no statement timeout, no cost estimation before execution. |
| **Discovery UX for wide schemas** | Hundreds of models need pagination, tagging, and ranked relationship search. |
| **Source nodes not yet included** | `catalog.sources` is ignored; FKs pointing at raw sources are dropped. |
| **No `--select` / dbt selector support** | Model filtering is regex-only today. |
