# Roadmap

Centralized tracking for all planned features. Items are grouped by
priority (P0 → P3) and ordered within each group. Identifiers like
`Sec-E` are retained as historical references where they appear in
commits or code comments, but the **grouping is by priority, not by
topic** — pagination, audit, and policy items live wherever their
priority places them.

---

## Status Overview

| Priority | Item | Status |
|---|---|---|
| P0 | Pagination — Relay-style cursor connections | 🔲 Planned |
| P0 | Query Allow-List | 🔲 Planned |
| P0 | Audit Logging | 🔲 Planned |
| P0 | MCP SOTA Surface (tools + resources + prompts) | 🔲 Pending |
| P1 | Few-shot Q→GraphQL example store | 🔲 Pending |
| P1 | dbt Selector Support (`--select`) | 🔲 Pending |
| P1 | Source Node Inclusion (`catalog.sources`) | 🔲 Pending |
| P1 | Hot reload of `access.yml` | 🔲 Planned |
| P1 | Policy test harness + `policy explain` CLI | 🔲 Planned |
| P2 | Column classifications | 🔲 Planned |
| P3 | Python extension hooks (Superset-style overrides file) | 🔲 Placeholder |
| ✅ | dbt-colibri delegation | Done |
| ✅ | Relationship derivation polish | Done |
| ✅ | MCP live enrichment | Done |
| ✅ | Docs + env-var config | Done |
| ✅ | Config-driven CLI + HTTP MCP transport | Done |
| ✅ | Identity & JWT Auth | Done |
| ✅ | RBAC + Column-Level Security | Done |
| ✅ | Allow / Deny effect (IAM-style precedence) | Done |
| ✅ | Row-Level Security | Done |
| ✅ | Data Masking | Done |
| ✅ | Structured row-filter DSL | Done |
| ✅ | Caching & burst protection | Done |
| ✅ | Pool admission control | Done |

---

## P0 — Next up

### Pagination — Relay-style cursor connections

Today's `query_max_limit` is a hard ceiling on inline `limit:` /
`first:` literals — it bounds a single response, but gives the agent
no way to *resume* past it. Pagination is the missing half:
cooperative iteration over a result set the cap forbids in one shot.

**Reference points (SOTA in GraphQL data APIs):**

| Project | Shape | Notes |
|---|---|---|
| **Relay spec** | `TypeConnection { edges { node, cursor } pageInfo { startCursor, endCursor, hasNextPage, hasPreviousPage } }` with `first/after` (forward) and `last/before` (backward) | The de facto standard. Apollo, PostGraphile, Hot Chocolate, GraphQL.js all align here. |
| **Hasura** | Offset-based (`limit`, `offset`) by default; cursor-style achieved via `where: { id: { _gt: $last_id } } order_by: { id: asc }` | Permission-table cap (`limit` on the role) acts as a hard per-request ceiling — analogous to our `query_max_limit`. |
| **GraphJin** | Native cursor pagination — generates opaque base64 cursors from the `order_by` columns; `first`/`last`/`after`/`before` args. Per-table `default_limit` + `max_limit` config. | Closest to what dbt-graphql wants: declarative, no resolver code per table. |
| **PostGraphile** | Full Relay connections; cursors derived from primary key + order. | Same pattern as GraphJin, more verbose schema. |

**Plan:**

1. **Adopt Relay-style connections** as a new optional resolver shape
   per table. The plain list form (`customers(limit, where) { ... }`)
   stays as the simple path; opting into pagination produces a
   *Connection* type (`customers_connection { edges { node, cursor }
   pageInfo { ... } }`) selectable instead of the plain list.
2. **Cursor encoding.** Opaque base64 of `(order_by_value, primary_key)`
   tuples — same approach as GraphJin/PostGraphile. The primary key
   tiebreaker makes the cursor stable across rows that share the
   `order_by` value. Cursors are signed with an HMAC keyed off the
   process secret so a client can't forge one to skip forward into
   policy-restricted territory.
3. **Args.** `first: Int, after: String` (forward) and `last: Int,
   before: String` (backward). Combined invariants per the Relay spec
   (no mixing forward+backward). `where:` and `order_by:` continue to
   apply.
4. **Default `order_by`.** Required for cursor stability — without it
   the warehouse can return a different page order each call. When the
   schema can derive a primary key from constraints (already extracted
   for relationships), default to `order_by: { <pk>: asc }`. Tables
   without a discoverable PK must declare an explicit `order_by`
   argument or the resolver returns a structured error.

**Coordination with `query_max_limit`:**

The cap is the **per-page ceiling** — it does not change meaning when
pagination lands:

| Args supplied | Resolver behaviour |
|---|---|
| Neither `limit:` nor `first:` | Resolver injects `first: query_max_limit` automatically. The "unbounded" `{ customers { id } }` becomes a 1000-row first page; the agent must opt into more rows by paginating. (Mirrors Hasura's per-role default-limit + GraphJin's `default_limit`.) |
| `first: N` (or `limit: N`) with N ≤ cap | Honoured as-is. |
| `first: N` (or `limit: N`) with N > cap | Validation rule rejects with `MAX_LIMIT_EXCEEDED` *before* execution — same as today. |
| Variable `$n` in `first:` / `limit:` | Validation can't see the value; resolver applies `min($n, cap)` at execution and tags the response with `extensions.truncated_to: <cap>` so the client knows the page was capped. |

The total-scan question ("agent walks 100 pages") is answered by
**cooperative iteration plus auth backoff**, not a second cap:

- Cursors give an explicit `pageInfo.hasNextPage: false` termination
  signal — agents stop when the data ends, not when a magic number hits.
- Each page is a normal request, so it's already covered by
  `db.pool.timeout` admission and the result cache. Bursty agent
  loops degrade through 503+`Retry-After`, not via a new knob.
- A `query_max_total_pages` would be a tempting third cap but adds
  little: the agent can launch a second connection and continue from
  the last cursor. Keep limits *per request* and let admission control
  shape *across requests*.

**Why the per-page cap remains useful even after pagination:**

- It prevents the trivial "single shot, give me everything" exploit
  (`first: 10000000`) that pagination *enables* if you forget to cap
  the page.
- It makes default behaviour (no args) safe — without the cap, the
  resolver would have to either (a) refuse to execute or (b) silently
  pick a default. Hasura/GraphJin both pick (b); we'd do the same and
  the cap is the operator-tunable value of that default.
- It bounds memory + warehouse cost for any *one* query so admission
  control can reason about budget per checkout, not per session.

**Files / scope (sketch, not committed):**
- `src/dbt_graphql/formatter/graphql.py` — emit `XConnection` /
  `XEdge` / `PageInfo` SDL when a table opts in.
- `src/dbt_graphql/graphql/resolvers.py` — new connection resolver path
  alongside the plain-list resolver.
- `src/dbt_graphql/compiler/query.py` — translate `(after, first)` into
  `WHERE (order_value, pk) > (after_order_value, after_pk) ORDER BY ...
  LIMIT first+1` (the +1 produces `hasNextPage` without a second
  query).
- `src/dbt_graphql/graphql/cursors.py` — HMAC-signed encode/decode.
- `docs/pagination.md` — operator + agent guide, including how the
  default-limit injection composes with `query_max_limit`.

---

### Query Allow-List (Sec-E)

**Motivation:** In production, anonymous or compromised clients should not be able to explore the schema via ad-hoc queries. Allow-lists lock the API to known query shapes, preventing introspection and injection of novel query patterns.

**Config additions (`config.yml`):**
```yaml
security:
  production: false           # true → allow-list enforcement
  allowlist_path: "allowlist.json"
```

**Behavior:**
- **Dev mode** (`production: false`): every executed query's normalized hash is appended to `allowlist.json` (upsert by hash).
- **Production mode** (`production: true`): queries not in `allowlist.json` are rejected with HTTP 403 before resolver execution.
- Hash = SHA256 of the normalized query string (stripped of whitespace, field order-normalized via GraphQL AST).

**CLI additions:**
```bash
dbt-graphql serve --production              # enforce allow-list
dbt-graphql allowlist list                  # print recorded queries + hashes
dbt-graphql allowlist clear                 # wipe allowlist.json
dbt-graphql allowlist add --query "{ ... }" # manually add a query
```

**Files to create/modify:**
- `src/dbt_graphql/api/allowlist.py` — `AllowListManager`: hash normalization, record, enforce
- `src/dbt_graphql/api/app.py` — middleware: check allowlist before resolver dispatch
- `src/dbt_graphql/cli.py` — `--production` flag; `allowlist` subcommand

| Item | Status |
|---|---|
| GraphQL AST normalization + SHA256 hash | 🔲 |
| Allow-list JSON persistence (append/upsert) | 🔲 |
| Dev mode recorder middleware | 🔲 |
| Production mode enforcement middleware (403 on miss) | 🔲 |
| `allowlist` CLI subcommand | 🔲 |

---

### Audit Logging (Sec-F)

**Motivation:** Compliance and forensics. Who accessed what, when, with what filters applied — essential for GDPR, SOC2, and data governance reviews.

**Emitted per request (structured log + OTel span attributes):**
```json
{
  "event": "graphql_query",
  "user_id": "usr_123",
  "user_email": "alice@acme.com",
  "effective_roles": ["analyst"],
  "tables_accessed": ["orders", "customers"],
  "columns_requested": 12,
  "columns_masked": 2,
  "columns_blocked": 1,
  "row_filter_applied": true,
  "query_hash": "sha256:abc123...",
  "allow_listed": true,
  "duration_ms": 42,
  "error": null
}
```

**Files to create/modify:**
- `src/dbt_graphql/api/audit.py` — `AuditEvent` dataclass, `emit_audit_event()`
- `src/dbt_graphql/api/resolvers.py` — populate and emit `AuditEvent` per resolver call
- Hooks into existing OTel tracer — adds audit fields as span attributes on the active span

| Item | Status |
|---|---|
| `AuditEvent` dataclass | 🔲 |
| Emit via loguru + OTel span attributes | 🔲 |
| Per-resolver instrumentation | 🔲 |
| Mask/block counts propagated from policy evaluation | 🔲 |

---

### MCP SOTA Surface

**Files to create/modify:**
- `src/dbt_graphql/mcp/syntax.py` — static syntax doc
- `src/dbt_graphql/mcp/search.py` — lexical table search
- `src/dbt_graphql/mcp/resources.py` — MCP resource handlers
- `src/dbt_graphql/mcp/prompts.py` — MCP prompt template
- `src/dbt_graphql/mcp/server.py` — register new tools, resources, prompts

| Item | Status |
|---|---|
| `get_query_syntax()` tool — static Markdown ≤ 2KB of dialect guide | 🔲 |
| `search_tables(query, limit)` tool — `difflib` lexical scoring against name + description | 🔲 |
| MCP Resource `schema://overview` — one line per table, no DB call | 🔲 |
| MCP Resource `schema://table/{name}` — calls `describe_table`, renders markdown | 🔲 |
| MCP Resource `schema://examples` — renders `examples.yml`; empty if missing | 🔲 |
| MCP Prompt `explore_and_query(goal)` — multi-turn stub | 🔲 |
| `suggest_examples` tool stub wired (impl in few-shot store) | 🔲 |

**Testing:**
- `get_query_syntax` response < 2KB
- `search_tables("order")` ranks `orders` and `stg_orders` first on jaffle-shop fixture
- `schema://overview` resource includes every table name

---

## P1 — Soon

### Few-Shot Q→GraphQL Example Store

**Files to create/modify:**
- `src/dbt_graphql/mcp/examples.py` — loader + lexical retriever
- `src/dbt_graphql/config.py` — `examples_path: Path | None = None` on `AppConfig`
- `playground/examples.yml` — 3–5 examples against playground schema

| Item | Status |
|---|---|
| `examples.yml` format (`question`, `query`, `tags`) | 🔲 |
| `load_examples(path)` — missing file → `[]`, no crash | 🔲 |
| `retrieve(question, examples, limit)` — `difflib` + tag overlap bonus | 🔲 |
| `suggest_examples(question)` tool implemented | 🔲 |
| YAML round-trip test | 🔲 |
| Tag overlap bumps score | 🔲 |

---

### dbt Selector Support (`--select` / `--exclude`)

**Motivation:** Large dbt projects use schema-per-team layouts, exposures tied to specific dashboards, or node graph traversal (`+orders`, `tag:finance`) to define meaningful subsets of the model graph. A simple regex on model names can't express these patterns.

**Approach:** Shell out to `dbt ls` with the user-provided selector string and let dbt resolve the node set. Feed the resulting model names as an allowlist into `extract_project`.

```bash
dbt-graphql generate \
  --catalog target/catalog.json \
  --manifest target/manifest.json \
  --select "tag:finance,+orders"
  --project-dir .
```

**Implementation:**
1. Add `--select` / `--project-dir` CLI flags (alongside existing `--exclude`).
2. Run `dbt ls --select <selector> --output json --profiles-dir <dir>`.
3. Parse JSON output → set of selected node unique IDs.
4. In `extract_project`, skip catalog nodes not in that set.

---

### Source Node Inclusion (`catalog.sources`)

**Motivation:** FK relationships pointing to a dbt source table are silently dropped because `extract_project` only iterates `catalog.nodes` and skips `catalog.sources`.

**Approach:** Iterate `catalog.sources` in addition to `catalog.nodes`. Create `ModelInfo` entries for source tables that are FK targets of selected models. Mark them as read-only.

**Scope:**
- Extend `extract_project` to iterate `catalog.sources`.
- Extend `build_relationships` to resolve source node unique IDs (`source.*`).
- Formatter and SQL compiler already work generically via table names — minimal changes needed.

---

### Hot reload of `access.yml` (Sec-K)

**Motivation:** Role/claim changes shouldn't require a full API restart.
Watch the file, rebuild the engine, swap it atomically.

| Item | Status |
|---|---|
| `watchfiles`-based observer in the API lifespan | 🔲 |
| Atomic swap of `PolicyEngine` reference on reload | 🔲 |
| Reload-failed fallback: keep previous engine, log loud error | 🔲 |
| OTel counter `policy.reload.{success,failure}` | 🔲 |

---

### Policy test harness + `policy explain` CLI (Sec-L)

**Motivation:** Policy is code — it should have tests. Give operators a
CLI to explain what a given JWT would see against a given table, and
inline test blocks to run in CI.

**Inline tests in `access.yml`:**
```yaml
tests:
  - name: analyst sees their org only
    given:
      jwt: { groups: [analysts], claims: { org_id: 7 } }
      table: customers
    expect:
      allowed_columns: any
      blocked_columns: []
      masks: { email: "CONCAT(...)" }
      row_filter_contains: "org_id"
```

**CLI:**
```bash
dbt-graphql policy explain --jwt '{"sub":"u1","groups":["analysts"]}' --table customers
dbt-graphql policy test         # runs inline tests, CI-friendly exit code
```

| Item | Status |
|---|---|
| `policy explain` CLI subcommand | 🔲 |
| `tests:` schema + runner | 🔲 |
| `policy test` exit code + structured failure output | 🔲 |
| Playbook of recipes in docs/access-policy.md | 🔲 |

---

## P2 — Later

### Column classifications (Sec-I)

**Motivation:** Today a mask rule lives on every `policy × table × column`
cell. For a 50-table schema with 5 PII columns each, that's 250 duplicated
mask strings. Classifications collapse this to one mask per class.

**Policy additions:**
```yaml
classifications:
  pii:
    mask: "CONCAT('***@', SPLIT_PART({col}, '@', 2))"   # {col} = column ref
  pii_strict:
    mask: "NULL"

columns:
  customers.email: [pii]
  customers.ssn:   [pii_strict]

policies:
  - name: analyst
    when: "'analysts' in jwt.groups"
    tables: { customers: { column_level: { include_all: true } } }
    respects: [pii, pii_strict]   # both masks apply
  - name: admin
    when: "'data-admins' in jwt.groups"
    tables: { customers: { column_level: { include_all: true } } }
    respects: []                  # bypass all classifications
```

**Complementary source:** classifications may also be read from dbt `meta`
(e.g. `meta.dbt_graphql.classification: pii`) so model owners mark the
sensitivity at the dbt layer. This is the same split Immuta and Collibra
use — classification lives with the data, role-to-classification binding
lives with the application.

| Item | Status |
|---|---|
| `classifications:` loader | 🔲 |
| `columns:` tag map + `respects:` on policies | 🔲 |
| Mask template placeholder `{col}` rendered per column | 🔲 |
| Read classifications from dbt `meta.dbt_graphql.classification` | 🔲 |

---

## P3 — Placeholder

### Python Extension Hooks (Sec-M)

**Motivation:** Several features need user-supplied callables that don't
fit cleanly into YAML — JWT key resolvers backed by Vault/KMS/HSM,
custom mask functions, custom audit sinks, custom cache backends. Today
each feature would invent its own dotted-path string in YAML, which
is config-as-code laundered through a string and gives up
discoverability and static checking.

**Approach (sketch):** Superset's `superset_config.py` pattern — a
single Python file the operator owns, evaluated at startup, where they
register hooks via a stable extension API. Solves once for all
extension points instead of per-feature.

This is a placeholder. A full plan lands when the first feature
actually needs it (likely exotic JWT key sources or custom masks).
Until then, all extension surfaces stay declarative and YAML-only.

---

## Notes — Security & Governance design principles

The two primary references for the policy engine:

- **Cube.dev Access Policies** — member-level (column) access, row-level filters, data masking; declarative YAML policies evaluated per request against JWT `securityContext`; OR semantics across multiple matching roles.
- **GraphJin Production Security** — RBAC with role-table-operation bindings; compile-time row filter injection; column allowlists; production query allow-lists that prevent ad-hoc query execution.

**Design principles:**
1. **Compile-time enforcement** — row filters and masking are injected into SQL at query-compile time, not post-processed in Python. They cannot be bypassed or leaked.
2. **Declarative** — all policy lives in `access.yml` alongside `db.graphql`; no code changes per policy update.
3. **Context-driven** — JWT claims drive dynamic filtering (`$jwt.sub`, `$jwt.claims.region`).
4. **OR semantics** — if a user matches multiple roles, the most permissive applicable policy wins (additive access).

---

## ✅ Shipped

### dbt-colibri Delegation

| Item | Status |
|---|---|
| `dbt-colibri>=0.3.0,<0.4` dep added | ✅ |
| `prepare_scope` + `to_node` wired | ✅ |
| Single-pass `_extract_both()` for column lineage + JOIN mining | ✅ |
| `ColumnLineageEdge` deleted; typed graph edges | ✅ |
| Lineage type normalization (`pass-through` → `pass_through`) | ✅ |
| `remove_quotes`/`remove_upper` for Postgres/BigQuery dialects | ✅ |
| `source_model` → `unique_id` for cross-package disambiguation | 🔲 Deferred until multi-package projects are encountered |
| Snapshot test against baseline `lineage.json` | 🔲 |
| New adapter fixtures (Postgres CamelCase, BigQuery backtick, UNNEST, two-package) | 🔲 |

---

### Relationship Derivation Polish

| Item | Status |
|---|---|
| `from_columns`/`to_columns` lists on `RelationshipInfo` | ✅ |
| `cardinality_confidence`, `business_name`, `description` on `RelationshipInfo` | ✅ |
| `ColumnInfo.is_primary_key` | ✅ |
| `RelationshipOrigin.join_hint` | ✅ |
| `constraints.py`: sqlglot-based FK parsing, composite FK support | ✅ |
| `data_tests.py`: reads `meta.relationship_name` + `meta.description` | ✅ |
| Self-join fix | ✅ |
| `join_hint` downgrade in `pipeline._rel_to_domain` | ✅ |
| `@relation` directive: `origin`, `confidence`, `name`, `description`, composite `fields`/`toFields` | ✅ |
| `compiler/query.py`: composite FK predicate with `and_(...)` | ✅ |

---

### MCP Live Enrichment

| Item | Status |
|---|---|
| `EnrichmentConfig` in `config.py` | ✅ |
| `describe_table` wired to `_get_row_count` + `_get_sample_rows` | ✅ |
| Per-column `value_summary`: enum / date-range / distinct-values | ✅ |
| Budget semaphore limiting live DB queries | ✅ |
| `catalog.json` stats preferred over live `COUNT(*)` | ✅ |
| `enrichment.budget` config field (env-overridable) | ✅ |
| Unit tests (no-DB path returns nulls) | ✅ |
| Integration test (Postgres + MySQL): distinct values + row_count + sample_rows | ✅ |
| Cache: second call returns same object | ✅ |

---

### Docs + Env-Var Config

| Item | Status |
|---|---|
| `pydantic-settings>=2.0` dep | ✅ |
| `AppConfig → BaseSettings`, `env_prefix="DBT_GRAPHQL__"`, `env_nested_delimiter="__"` | ✅ |
| Precedence: init > env > file > defaults | ✅ |
| `docs/mcp.md` | ✅ |
| `docs/configuration.md` | ✅ |
| `docs/architecture.md` updates | ✅ |
| `docs/access-policy.md` | ✅ |
| `config.example.yml` at repo root (commented Helm-style defaults) | ✅ |
| Defaults centralized in `defaults.py` | ✅ |

---

### Config-driven CLI + HTTP MCP Transport

| Item | Status |
|---|---|
| `DbtConfig` (`catalog`, `manifest`, `exclude`) in `AppConfig` | ✅ |
| Flat CLI: `--config` + `--output` (no subcommands) | ✅ |
| Relative path resolution for `catalog`/`manifest` from config dir | ✅ |
| Flat `serve` config: `mcp_enabled` + `graphql_introspection`; GraphQL always-on in serve mode | ✅ |
| `build_registry(project)` — direct `ProjectInfo → TableRegistry` (no SDL roundtrip) | ✅ |
| `GraphQLBundle` shared with MCP (same schema, same per-request context) | ✅ |
| Streamable HTTP MCP transport via FastMCP | ✅ |
| Single `serve.run()` Granian entry — GraphQL always, MCP opt-in | ✅ |
| Unified auth + policy for GraphQL and MCP (`run_graphql` tool replaces raw SQL) | ✅ |
| `api`/`mcp` optional extras collapsed into core deps | ✅ |
| `redis` optional extra for Redis cache backend | ✅ |
| `timed` async context manager in `monitoring.py` (shared OTel recording) | ✅ |

---

### Identity & JWT Auth (Sec-A)

**Design — OAuth 2.0 Resource Server.** dbt-graphql is a **Resource
Server**, not an Authorization Server. An external identity provider
(Auth0 / Keycloak / Cognito / Clerk / Cube / a custom service) issues
signed JWTs; we verify the signature, read the payload, and evaluate
policy against it. We never handle credentials, never issue tokens,
never call a login endpoint. Translation/exchange (opaque token → JWT,
session cookie → JWT, mTLS → JWT) belongs in a reverse proxy or a
sidecar service that sits in front of us — from our POV the wire
format is always `Authorization: Bearer <jwt>`.

**Reference:** [`docs/security.md`](docs/security.md),
[`docs/configuration.md#securityjwt`](docs/configuration.md).

Library: **joserfc** for JWS/JWT/JWK primitives (Authlib successor by
the same author; `authlib.jose` is deprecated, PyJWT lacks first-class
JWKS rotation). Key sources: `jwks_url` (rotating set) | `key_url` |
`key_env` | `key_file` (mutually exclusive, validated at config load).
`enabled: false` skips verification entirely and treats every request
as anonymous — there is no trust-only / signature-skipping decode mode.

**Explicitly out of scope:**
- API keys — resource servers don't mint credentials. If a caller
  needs a long-lived token, they get one from the Authorization Server
  and send it as a JWT. A middleware in front of us can translate API
  keys to JWTs on the fly.
- `anonymous_role` config — "no/invalid token" is already expressible
  in policy via `when: "jwt.sub == None"`. No config wiring needed.
- Login / password / session handling — Authorization Server concern.
- Programmatic / callable key resolvers — deferred to the Python
  extension hooks placeholder, where Vault/KMS/HSM integration is
  solved once for all extension points.

| Item | Status |
|---|---|
| `JWTConfig` Pydantic schema with mutually-exclusive key source validation | ✅ |
| `auth/` package: `backend.py`, `verifier.py`, `keys.py` | ✅ |
| `StaticKeyResolver` (env / file / url) + joserfc verifier | ✅ |
| Algorithm allow-list pinning (alg-confusion regression test) | ✅ |
| `exp` / `nbf` / `aud` / `iss` / `required_claims` validation with `leeway` | ✅ |
| RFC 6750 fail-closed: 401 + `WWW-Authenticate: Bearer error="invalid_token"` | ✅ |
| `JWKSResolver` (httpx async + monotonic TTL + asyncio.Lock coalescing) | ✅ |
| Configurable `roles_claim` for scope extraction (defaults to `scope`) | ✅ |
| OTel `auth.jwt` counter with outcome attribute | ✅ |
| `JWTPayload` dot-access available in `when:` and `row_filter` `{ jwt: ... }` references | ✅ |
| HTTP integration tests for policy + JWT (PostgreSQL + MySQL) | ✅ |

---

### RBAC + Column-Level Security (Sec-B)

The shipped engine uses `policies[*].when` (simpleeval expressions
against the JWT) rather than the originally-drafted `match_groups` lists —
`when` subsumes group matching and adds arbitrary claim predicates. Column
access is union-OR across matching policies (most-permissive wins). Default
is **deny** at the table level and **strict** at the column level — any
table not covered by an active policy, or any column not authorized by the
merged policy, produces a structured GraphQL `FORBIDDEN_TABLE` /
`FORBIDDEN_COLUMN` error (see `docs/access-policy.md#error-responses`).

Policy enforcement is applied at **every table reached by the query**,
including tables pulled in through nested GraphQL relations — so a nested
selection cannot bypass deny / strict / mask / row-filter.

**Reference:** [`docs/access-policy.md`](docs/access-policy.md),
[`access.example.yml`](access.example.yml).

| Item | Status |
|---|---|
| `access.yml` Pydantic schema (`AccessPolicy`, `PolicyEntry`, `TablePolicy`, `ColumnLevelPolicy`) | ✅ |
| `when` evaluation via `simpleeval` (dunder + builtin sandbox) | ✅ |
| `include_all` / `includes` / `excludes` merge (OR semantics) | ✅ |
| Column stripping in `compile_query` via `ResolvedPolicy` | ✅ |
| `security.policy_path` config + `load_access_policy` | ✅ |
| `access.example.yml` | ✅ |
| Table-level default-deny (unlisted table → `FORBIDDEN_TABLE`) | ✅ |
| Strict columns (unauthorized column → `FORBIDDEN_COLUMN`, not silent strip) | ✅ |
| Nested-relation policy enforcement (columns / masks / row filters) | ✅ |
| Structured GraphQL error extensions (`code`, `table`, `columns`) | ✅ |
| `--policy PATH` CLI override of `config.security.policy_path` | 🔲 |

---

### Allow / Deny effect (Sec-G)

Each `PolicyEntry` carries a required `effect: allow | deny` field —
the standard XACML / AWS IAM convention (`Allow`/`Deny`), synonymous
with Cedar's `permit`/`forbid` and SQL Server's `GRANT`/`DENY`. Allows
merge OR-style; denies take precedence (deny always wins). Allow rules
declare `column_level` and/or `row_filter`; deny rules declare
`deny_all` (full table) and/or `deny_columns` (column subtraction).
Mixing the two field sets in one entry fails policy load.

**Reference:** [`docs/access-policy.md`](docs/access-policy.md) §
*`effect`*.

The motivation is cross-cutting prohibitions like *"contractors never
see PII even if they're also analysts"* — a guard that's impossible to
keep correct when written into every allow rule's `when` clause but
trivial as a single deny rule.

| Item | Status |
|---|---|
| `Effect` enum + required `effect` field on `PolicyEntry` | ✅ |
| `deny_all` / `deny_columns` fields on `TablePolicy` | ✅ |
| Effect-vs-fields validator (rejects mixed shapes at load) | ✅ |
| Engine: deny short-circuit on full-table denial; column subtraction; mask drop | ✅ |
| Load-time `deny_columns` validation against `TableRegistry` | ✅ |

---

### Row-Level Security (Sec-C)

Row filters are Hasura-style structured DSL trees (`row_filter`)
compiled directly to SQLAlchemy `ColumnElement` clauses. Column names are
validated against the table registry at policy-load time; JWT claim values
bind as named parameters via `bindparam`. SQL injection via JWT claims is
structurally impossible. OR semantics across matching policies.

**Reference:** [`docs/access-policy.md`](docs/access-policy.md) §
*`row_filter` reference*.

| Item | Status |
|---|---|
| Hasura-style DSL (`_eq`, `_and`, `_or`, `_not`, `_in`, `_is_null`, …) | ✅ |
| Compile to SQLAlchemy `ColumnElement` (no raw SQL strings) | ✅ |
| Load-time column-reference validation against table registry | ✅ |
| OR merge across matching policies (per-policy name prefix) | ✅ |
| Merge with user `where:` in `compile_query` | ✅ |
| SQL injection regression test | ✅ |
| `{ jwt: <dotted.path> }` references (missing claim → SQL NULL → default-deny) | ✅ |

---

### Data Masking (Sec-D)

Mask expressions are raw SQL fragments from `access.yml`
(operator-controlled, trusted). `"NULL"` emits a bound SQL NULL; anything
else goes through `literal_column(...).label(col)`. Multi-policy mask merge
applies only when every matching policy masks the column and agrees on the
expression; conflicting expressions raise at evaluate time.

| Item | Status |
|---|---|
| Mask expression resolution (union of matching policies) | ✅ |
| SQL mask injection in `compile_query` (`_mask_column`) | ✅ |
| `NULL` static mask | ✅ |
| "Least-masked wins" — any unmasked matching policy drops the mask | ✅ |
| Conflict detection (structured `POLICY_MASK_CONFLICT` GraphQL error) | ✅ |
| Dialect safety: reject `;`, `--`, `/*`, `*/` in mask strings at load time | ✅ |

---

### Structured row-filter DSL (Sec-H)

The DSL is the only row-filter language. Hasura convention
(`_eq`, `_and`, `_or`, `_not`, `_in`, `_is_null`, …); RHS values are
literals or `{ jwt: <dotted.path> }` references. The compiler emits
SQLAlchemy `ColumnElement` clauses that drop directly into
`stmt.where(...)`. There is no template engine in the data-access path.
Load-time validation rejects mixed logical/column keys at one node
(would silently drop a branch at compile time) and `NULL` in `_in`
lists (`SQL IN (NULL)` never matches — operator must use `_is_null`).

**Policy form:**
```yaml
tables:
  customers:
    row_filter:
      _and:
        - org_id: { _eq: { jwt: claims.org_id } }
        - _or:
            - is_public: { _eq: true }
            - owner_id: { _eq: { jwt: sub } }
```

| Item | Status |
|---|---|
| DSL Pydantic schema and load-time structural validation | ✅ |
| Compiler → SQLAlchemy `ColumnElement` (no raw SQL strings) | ✅ |
| Load-time column validation against `TableRegistry` | ✅ |
| OR-merge across matching policies | ✅ |

---

### Caching & Burst Protection (Sec-J)

Result cache + singleflight via cashews protects the warehouse from
bursts of **identical** concurrent queries. Single backend URI: in-mem
by default, or any Redis URI (incl. Redis Cluster) for shared state
across replicas — operators provide the URI; cluster stability is the
backend's concern, not ours. The `cache.result` OTel counter emits
per-outcome attributes (`hit` / `coalesced` / `miss`).
Reference: [`docs/caching.md`](docs/caching.md).

Operator-rejected escape hatches: the 203 short-circuit on lock-wait
was deferred — clients time themselves out, and the lock-wait is
already bounded by `cache.lock_safety_timeout`.

---

### Pool Admission Control (Sec-N)

Companion to the cache: caching coalesces identical query bursts;
admission control admits / fast-fails **distinct** query bursts at the
pool boundary.

The SQLAlchemy pool *is* the admission queue. `db.pool` config
(`size`, `max_overflow`, `timeout`, `recycle`, `retry_after`) tunes
it. On checkout timeout the resolver raises a structured
`POOL_TIMEOUT` GraphQL error that a custom Ariadne HTTP handler
elevates to **HTTP 503 + `Retry-After`** so generic LB clients can
back off without parsing GraphQL bodies. Wait-time is observable via
the `db.client.connections.wait_time` OTel histogram with `outcome`
attribute (`acquired` / `timeout`). Pool depth comes for free from
OTel SQLAlchemy auto-instrumentation. See
[`docs/configuration.md#dbpool`](docs/configuration.md).

| Item | Status |
|---|---|
| `db.pool` config block + defaults | ✅ |
| Resolver-side `SAPoolTimeoutError` → `POOL_TIMEOUT` extension | ✅ |
| Ariadne `PoolAwareHandler` → 503 + Retry-After | ✅ |
| `db.client.connections.wait_time` histogram | ✅ |
| Cross-replica pool admission (warehouse-side concern) | 🔲 Out of scope — see plan §rationale |

---

## Open Deviations

| Item | Decision |
|---|---|
| Short names vs `unique_id` in lineage | Deferred — relevant only when multi-package projects are encountered |
| Row-filter template engine | Jinja2 `SandboxedEnvironment` with `finalize=` hook. Every `{{ expression }}` becomes a SQL bind param; values never hit the rendered SQL. |
| `when:` evaluator | `simpleeval` — AST-based, rejects dunders + builtins, keeps the Python-flavored syntax operators already use. |
| JWT verification | joserfc-backed signature + claims validation. JWKS rotating set or static key source. `enabled: false` skips verification entirely (dev only). |
