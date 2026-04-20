# Landscape & Comparison

Where dbt-graphql sits relative to other tools in the semantic-layer-for-AI space and the GraphQL-over-SQL space, plus an honest assessment of what it does well and what's missing.

For the architecture and design rationale, see [architecture.md](architecture.md).

---

## TL;DR

> dbt-graphql occupies the intersection of two mature spaces — GraphQL-over-SQL (Hasura, PostGraphile, pg_graphql) and AI semantic layers (Cube, MetricFlow, Wren, Malloy) — and picks the smallest viable middle: no new modeling language, no mutations, no separate metrics store. The bet is that for agent-driven analytics, a typed, lineage-aware, read-only GraphQL schema derived straight from dbt is the fastest path to correct answers.

---

## 1. Semantic layers for AI

The "semantic layer for AI" space consolidated through 2025. The common pattern: define business metrics and relationships once in a modeling language (MetricFlow, Cube schema, MDL, Malloy), then expose those definitions to agents via an MCP server or an AI-friendly API. dbt-graphql sits adjacent to that space but takes a deliberately thinner approach: it does not ask you to author a second modeling layer — it treats your existing dbt project (manifest + catalog) as the source of truth and exposes it through a GraphQL schema and an MCP server.

| Project | Core idea | Source-of-truth model | AI / agent integration | Query language | DB support | License | Stack |
|---|---|---|---|---|---|---|---|
| **dbt-graphql** (this project) | Turn dbt artifacts into GraphQL + SQL compiler + MCP | dbt `manifest.json` + `catalog.json` (models, tests, relationships, constraints, lineage) | MCP server: schema discovery, join-path search, query build + execute | GraphQL (compiled to SQL via SQLAlchemy) | Any SQLAlchemy-supported warehouse | MIT | Python |
| **[Cube](https://github.com/cube-js/cube)** | Universal semantic layer for BI and AI | Cube data model (YAML/JS cubes, views, measures) | [Cube MCP server](https://cube.dev/docs/product/apis-integrations/mcp-server) over HTTPS with OAuth; "agentic analytics" platform | SQL API, REST, GraphQL, MDX; Cube compiles prompts deterministically to SQL | Postgres, BigQuery, Snowflake, Redshift, Databricks, 20+ | Apache-2.0 (core) / MIT (client) | Node.js + Rust |
| **[dbt Semantic Layer / MetricFlow](https://github.com/dbt-labs/metricflow)** | Governed metrics on top of dbt | MetricFlow YAML (semantic models, metrics) in the dbt project | JDBC + GraphQL Semantic Layer APIs; MCP connectors emerging; [open-sourced at Coalesce 2025](https://www.getdbt.com/blog/open-source-metricflow-governed-metrics) under Apache-2.0 | MetricFlow query spec (metrics, dimensions, filters) | Any dbt-supported warehouse | Apache-2.0 | Python |
| **[Wren Engine](https://github.com/Canner/wren-engine)** | Open context engine for AI agents, MDL-based | MDL (Model Definition Language) | Semantic engine for MCP clients; powers [WrenAI](https://github.com/Canner/WrenAI) GenBI agent | SQL (planned via Apache DataFusion from MDL) | 15+ sources (PG, BigQuery, Snowflake, DuckDB, …) | Apache-2.0 | Rust + DataFusion |
| **[Malloy](https://github.com/malloydata/malloy)** | A modern language for data relationships and transformations | Malloy source files (semantic model + queries in one language) | [Publisher](https://github.com/malloydata/publisher) semantic model server; VS Code extension | Malloy (compiled to SQL) | BigQuery, Snowflake, Postgres, MySQL, Trino/Presto, DuckDB | MIT | TypeScript (+ Python bindings) |
| **[Vanna AI](https://github.com/vanna-ai/vanna)** | RAG-powered text-to-SQL | Training data: DDL, docs, example Q/SQL pairs (vector store) | Python library; integrates with Streamlit, Flask, Slack | Natural language → SQL | PG, MySQL, Snowflake, BigQuery, Redshift, SQLite, Oracle, MSSQL, DuckDB, ClickHouse | MIT | Python |
| **[LangChain SQLDatabaseToolkit](https://docs.langchain.com/oss/python/integrations/tools/sql_database)** | Agent toolkit that introspects a DB and calls an LLM to write SQL | Live DB introspection (via SQLAlchemy) at query time | Tools for a LangChain agent (list tables, get schema, run query, check query) | Natural language → SQL | Anything SQLAlchemy supports | MIT | Python |
| **[LlamaIndex NLSQLTableQueryEngine](https://developers.llamaindex.ai/python/framework-api-reference/query_engine/NL_SQL_table/)** | Query engine that turns NL into SQL against a `SQLDatabase` | Live DB schema + optional table retriever for large schemas | Part of a LlamaIndex workflow / agent | Natural language → SQL | Anything SQLAlchemy supports | MIT | Python |

**Positioning.** dbt-graphql is not a semantic layer and does not try to be one. Cube, MetricFlow, Wren and Malloy all require you to author a second modeling artifact (cubes, MetricFlow YAML, MDL, or `.malloy`). dbt-graphql instead *derives* its interface from what dbt already knows — tables, columns, tests, `relationships` tests as foreign keys, primary-key constraints, and lineage — and publishes that as a typed GraphQL schema plus an MCP surface. The closest philosophical neighbour is `pg_graphql` (introspect the source of truth and emit a schema), but the source of truth here is the dbt project rather than the live database. Compared to LangChain/LlamaIndex/Vanna agent-side text-to-SQL, dbt-graphql gives the agent a *structured* interface (a GraphQL schema and an MCP catalog of joinable entities) instead of raw tables + prompt-engineering, which tends to collapse the error surface. It is read-only and MCP-first by design, which is the opposite direction from Hasura/AppSync (write-heavy, app-backend oriented).

---

## 2. GraphQL-to-SQL in open source

The GraphQL-over-SQL space is largely about *how the schema is produced*. Almost every project that generates a GraphQL API from a relational database does so by introspecting live database catalogs (Postgres `pg_catalog`, `information_schema`) and turning tables, foreign keys, and indexes into types, connections, and filter arguments. dbt-graphql's departure is that it introspects **dbt artifacts**, not the database — so the GraphQL schema reflects modeled relationships, contracts, and lineage rather than raw DDL.

| Project | Core idea | Schema source | DB support | Language | Maturity | License |
|---|---|---|---|---|---|---|
| **dbt-graphql** (this project) | Generate GraphQL SDL from dbt artifacts; compile queries to SQL; serve via MCP | dbt `manifest.json` + `catalog.json` | Any SQLAlchemy-supported warehouse | Python | Early | MIT |
| **[Hasura graphql-engine](https://github.com/hasura/graphql-engine)** | Instant realtime GraphQL over multiple databases with RBAC, subscriptions, event triggers | Live DB introspection + metadata (tracked tables/relationships) | Postgres, MS SQL, BigQuery, MongoDB, ClickHouse | Haskell (v2) / Rust (v3) | Production, large ecosystem | [Apache-2.0 core; EE components commercial](https://hasura.io/docs/2.0/enterprise/upgrade-ce-to-ee/) |
| **[PostGraphile](https://postgraphile.org)** | Low-effort, high-performance GraphQL API from a Postgres schema | [Live Postgres introspection](https://postgraphile.org/postgraphile/5/introspection/), backed by Grafast planner | Postgres only | TypeScript / Node.js | Production, mature | [MIT (Graphile Crystal monorepo)](https://github.com/graphile/crystal) |
| **[GraphJin](https://github.com/dosco/graphjin)** | "Automagical" GraphQL-to-SQL compiler, no-code / config-only | Auto-discovered DB schema + relationships; YAML config | Postgres, MySQL, MongoDB, SQLite, Oracle, MSSQL | Go | Active, smaller community | Apache-2.0 |
| **[pg_graphql](https://github.com/supabase/pg_graphql)** (Supabase) | Postgres extension exposing GraphQL via a SQL function | [Live Postgres introspection inside the DB](https://supabase.github.io/pg_graphql/) (`graphql.resolve()`); FKs become relationships | Postgres only | Rust (Postgres extension) | Production (powers Supabase GraphQL) | Apache-2.0 |
| **[Dgraph](https://github.com/dgraph-io/dgraph)** | Graph-native distributed database that speaks GraphQL (and DQL) | User-defined GraphQL SDL stored inside Dgraph — *not* over an RDBMS | Dgraph's own storage | Go | Production (ownership: Hypermode 2023 → Istari Digital 2025) | Apache-2.0 |
| **[AWS AppSync / Amplify](https://aws.amazon.com/appsync/)** | Managed serverless GraphQL with real-time + offline sync | Generated from DynamoDB key schema, or custom SDL with JS/VTL resolvers | DynamoDB, Aurora, OpenSearch, arbitrary data sources | Managed (AWS) | Production, enterprise | Proprietary |

**Positioning.** Every other project in the table either introspects the live database (Hasura, PostGraphile, pg_graphql, GraphJin), is graph-native (Dgraph), or asks you to hand-write SDL + resolvers (AppSync). dbt-graphql is the only one that treats **dbt** as the authoritative schema source. That choice has concrete implications:

- Relationship edges in the GraphQL schema come from dbt `relationships` tests, not DB foreign keys.
- Primary keys come from dbt `constraints`.
- Column descriptions come from dbt docs.
- **Lineage** is exposable as a first-class field on the schema — something no DB-introspection tool can do, because lineage doesn't exist in `pg_catalog`. It only exists in the transformation graph.

Two further differentiators are deliberate: dbt-graphql is **read-only** (no mutations — analytics workload only), and it is **MCP-first** (the primary consumer is an LLM agent, not a web app). The closest conceptual neighbour is `pg_graphql`, but dbt-graphql trades "runs inside Postgres" ergonomics for "works on any warehouse dbt supports, and reflects the model the analytics team actually maintains."

---

## 3. Honest assessment

### Strengths

| Strength | Why it matters |
|---|---|
| **dbt-native** — the dbt project *is* the schema | No second modeling layer to maintain. Docs, tests, and constraints are reused verbatim. |
| **GraphQL schema as a first-class artifact** | Emitted SDL is inspectable by humans and machines; strong typing with PascalCase types and `@sql` directives gives agents a deterministic target. |
| **Lineage built in** | Upstream/downstream model lineage can be exposed alongside the schema — structurally impossible for DB-introspection tools. |
| **MCP-first** | The MCP server exposes discovery, relationship search, query construction, and execution as distinct tools — matching how agents actually plan. Same positioning as Cube, Wren, and dbt Labs' own AI surfaces. |
| **Read-only by design** | Removes an entire class of write-path risk and simplifies the compiler. The target is a `SELECT` tree, not a full DML surface. |
| **Cross-warehouse via SQLAlchemy** | Dialect-portable SQL generation; a natural seam to add EXPLAIN / query-plan inspection later. |
| **Python + dbt's runtime** | Easy to extend, easy to embed in data teams that already speak Python, aligns with dbt's own stack. |
| **Correlated subqueries over LATERAL** | Works on engines without LATERAL (Apache Doris), no extra machinery for nested relations. |
| **Catalog-derived statistics** (potential) | Row counts and column stats from `catalog.json` can be surfaced to agents to guide planning — future enhancement. |

### Gaps & open work

| Gap | What it means |
|---|---|
| **No metrics / semantic layer** | No measures, no predefined aggregations, no joins-as-measures. Aggregation is limited to what GraphQL selection can express. For governed metrics, pair with MetricFlow or Cube. |
| **Read-only** (also a strength) | No mutations, writes, or upserts. Wrong tool for app backends — Hasura/PostGraphile remain the right call there. |
| **No row-level security / auth layer** | Hasura has permission rules, pg_graphql inherits Postgres RLS, AppSync has Cognito. dbt-graphql has no equivalent today; multi-tenant serving needs an auth + policy layer in front of (or inside) the serve path. |
| **Single-process Python serving** | The serve layer is async SQLAlchemy but still Python. Hasura/PostGraphile are Haskell/Rust/Node and handle high-throughput concurrent agent workloads more easily. |
| **Compiler feature coverage** | No filter/order on nested fields; `where` supports only equality; no operators, no aggregates. Multi-hop relation nesting is supported (recursive correlated subqueries, cycle-safe). |
| **Maturity** | Hasura, PostGraphile, and pg_graphql each have years of production hardening. dbt-graphql needs an integration-test corpus covering real dbt projects across dialects. |
| **GraphQL feature coverage** | No subscriptions, unions, federation, defer/stream. None are table stakes for analytics, but worth an explicit non-goals list so users aren't surprised. |
| **No query-cost / safety guardrails** | No default `LIMIT`, no statement timeout, no cost estimation before execution. A warehouse LLM-agent tool almost certainly wants these. |
| **Discovery UX for wide schemas** | A real dbt project has hundreds of models. The MCP `list` surface will need pagination, tagging (dbt tags / folders), and ranked relationship search to stay usable. |
| **Source nodes not yet included** | `catalog.sources` is ignored; FKs pointing at raw sources are dropped. On the [roadmap](../ROADMAP.md). |
| **No `--select` / dbt selector support** | Model filtering is regex-only today. dbt selector syntax (`+orders`, `tag:finance`) is on the [roadmap](../ROADMAP.md). |

---

## Sources

- Cube: [repo](https://github.com/cube-js/cube), [MCP docs](https://cube.dev/docs/product/apis-integrations/mcp-server), [MCP announcement](https://cube.dev/blog/unlocking-universal-data-access-for-ai-with-anthropics-model-context)
- dbt Labs: [MetricFlow repo](https://github.com/dbt-labs/metricflow), [open-source announcement](https://www.getdbt.com/blog/open-source-metricflow-governed-metrics), [Semantic Layer docs](https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl)
- Wren: [Wren Engine](https://github.com/Canner/wren-engine), [WrenAI](https://github.com/Canner/WrenAI), [dbt integration announcement](https://www.getwren.ai/post/wren-ai-launches-native-dbt-integration-for-governed-ai-driven-insights), [dbt guide](https://docs.getwren.ai/cp/guide/dbt)
- Malloy: [repo](https://github.com/malloydata/malloy), [Publisher](https://github.com/malloydata/publisher)
- Vanna AI: [repo](https://github.com/vanna-ai/vanna)
- LangChain: [SQL toolkit docs](https://docs.langchain.com/oss/python/integrations/tools/sql_database)
- LlamaIndex: [NLSQLTableQueryEngine](https://developers.llamaindex.ai/python/framework-api-reference/query_engine/NL_SQL_table/)
- Hasura: [repo](https://github.com/hasura/graphql-engine), [CE vs EE licensing](https://hasura.io/docs/2.0/enterprise/upgrade-ce-to-ee/)
- PostGraphile: [introspection docs](https://postgraphile.org/postgraphile/5/introspection/), [Crystal monorepo](https://github.com/graphile/crystal)
- GraphJin: [repo](https://github.com/dosco/graphjin), [cheatsheet](https://graphjin.com/posts/cheatsheet), [pkg.go.dev](https://pkg.go.dev/github.com/dosco/graphjin/core)
- pg_graphql: [repo](https://github.com/supabase/pg_graphql), [docs](https://supabase.github.io/pg_graphql/)
- Dgraph: [repo](https://github.com/dgraph-io/dgraph)
- AppSync: [product page](https://aws.amazon.com/appsync/), [FAQs](https://aws.amazon.com/appsync/faqs/)
- Apache DataFusion (Wren Engine's planner): [datafusion.apache.org](https://datafusion.apache.org/)
