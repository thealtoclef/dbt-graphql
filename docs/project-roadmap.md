# Project Roadmap

Centralized tracking for all planned features. Items are grouped by priority and ordered within each group. Shipped items are listed first.

---

## Shipped (v0.1.0)

| #   | Feature                                                                   | Status |
| --- | ------------------------------------------------------------------------- | ------ |
| 1   | dbt artifact parsing (`catalog.json` + `manifest.json`, dbt v1–v12)       | ✅     |
| 2   | SQL→GraphQL type mapping with automatic enum detection                    | ✅     |
| 3   | Relationship derivation (constraints > data tests > compiled SQL lineage) | ✅     |
| 4   | Column-level lineage via sqlglot + dbt-colibri                            | ✅     |
| 5   | GraphQL API with Ariadne (async, correlated subqueries)                   | ✅     |
| 6   | Multi-database support (PostgreSQL, MySQL, MariaDB, Doris)                | ✅     |
| 7   | JWT authentication (OAuth 2.0 Resource Server model)                      | ✅     |
| 8   | RBAC with allow/deny effects, column-level security                       | ✅     |
| 9   | Row-level security (Hasura-style structured filter DSL)                   | ✅     |
| 10  | Data masking (SQL expression injection at compile time)                   | ✅     |
| 11  | Result cache + singleflight (cashews, Redis for multi-replica)            | ✅     |
| 12  | Pool admission control (HTTP 503 + Retry-After)                           | ✅     |
| 13  | MCP server (7 tools: discovery, join paths, query execution)              | ✅     |
| 14  | OpenTelemetry instrumentation (traces, metrics, logs)                     | ✅     |
| 15  | Config-driven CLI with generate and serve modes                           | ✅     |
| 16  | HTTP MCP transport (Streamable HTTP via FastMCP)                          | ✅     |
| 17  | MCP live enrichment (row counts, sample rows, value summaries)            | ✅     |

---

## P0 — Next up

### Relay-style cursor pagination

- Adopt Relay-style connections as an optional resolver shape per table
- Opaque base64 cursors from `(order_by_value, primary_key)` tuples with HMAC signing
- `first/after` (forward) and `last/before` (backward) args
- `query_max_limit` becomes per-page ceiling; default injection for unbounded queries

### Query Allow-List

- Dev mode: auto-record normalized query hashes to `allowlist.json`
- Production mode: reject non-allowlisted queries with HTTP 403
- SHA256 hash of normalized query (stripped whitespace, field order-normalized)

### Audit Logging

- Per-request structured logs (user, roles, tables, columns, masks, filters, duration)
- Emitted via loguru + OTel span attributes
- Covers GDPR, SOC2, and data governance compliance needs

### MCP SOTA Surface

- `get_query_syntax()` tool — static dialect guide
- `search_tables(query, limit)` tool — lexical table search
- MCP Resources: `schema://overview`, `schema://table/{name}`, `schema://examples`
- MCP Prompt: `explore_and_query(goal)` — multi-turn stub

---

## P1 — Soon

### Few-shot Q→GraphQL example store

- `examples.yml` format with `question`, `query`, `tags`
- Lexical retriever (`difflib` + tag overlap bonus)
- `suggest_examples(question)` MCP tool

### dbt Selector support (`--select`)

- Shell out to `dbt ls` with user-provided selector string
- Feed resolved model names as allowlist into `extract_project`

### Source node inclusion

- Iterate `catalog.sources` alongside `catalog.nodes`
- Create `ModelInfo` entries for source tables that are FK targets

### Hot reload of access policies

- `watchfiles`-based file observer
- Atomic swap of `PolicyEngine` reference on reload
- Reload-failed fallback: keep previous engine, log error

### Policy test harness + `policy explain` CLI

- Inline test blocks in config (`tests:` schema)
- `policy explain --jwt '...' --table customers` CLI
- `policy test` — CI-friendly exit code + structured failure output

---

## P2 — Later

### Column classifications

- Reusable sensitivity classes (`pii`, `pii_strict`) with mask templates
- `classifications:` loader + `columns:` tag map
- `respects:` field on policies to bind role-to-classification
- Optional read from dbt `meta.dbt_graphql.classification`

---

## P3 — Placeholder

### Python extension hooks

- Superset-style `dbt_graphql_config.py` overrides file
- Stable extension API for custom JWT key resolvers, masks, audit sinks, cache backends
- Deferred until the first feature actually needs it

---

## Architectural follow-ups

| ID      | Item                                                                                                                                                                                                                      | Priority |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| **F.1** | **DataLoader-style sibling subquery batching** — Batch sibling relations into `WHERE id IN (...)` lookups. 2–5× warehouse concurrency reduction for result sets with repeated relations. Highest leverage remaining work. | High     |
| **F.2** | **Query cost/complexity scoring** — `max_depth` exists but no complexity scoring. Prevents deeply nested queries that compile to expensive SQL. Pair with pool admission control.                                         | Medium   |
| **F.3** | **Persisted queries / APQ** — Apollo-defined spec for client-supplied hashes on subsequent calls. Shares structure with Query Allow-List (Sec-E).                                                                         | Medium   |
| **D.3** | **MCP HTTP transport e2e tests** — Protocol-level test harness for tool listing, invocation, resources, and prompts. Bake before Phase 3 adds new tools.                                                                  | Medium   |

---

## Recommended sequencing

1. **F.1 DataLoader spike** — Design doc, spike, ship. Highest leverage of remaining work.
2. **D.3 MCP HTTP harness** — Bake before Phase 3 (SOTA MCP surface) lands new tools.
3. **F.2 / F.3** — Opportunistic, paired with Query Allow-List (P0) when that ships.

---

## Notes

- Full historical details for each shipped item are in [ROADMAP.md](../ROADMAP.md).
- Architectural follow-ups and test gaps are in [FOLLOWUPS.md](../FOLLOWUPS.md).
- Feature-specific design docs are linked from the individual roadmap sections in `ROADMAP.md`.
