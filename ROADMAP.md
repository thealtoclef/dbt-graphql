# Roadmap

Centralized tracking for all planned features. Sections are ordered by priority within each group.

---

## Status Overview

| Phase | Feature | Status |
|---|---|---|
| 0 | dbt-colibri delegation | ✅ Done |
| 1 | Relationship derivation polish | ✅ Done |
| 2 | MCP live enrichment | ✅ Done |
| 3 | MCP SOTA surface (tools + resources + prompts) | 🔲 Pending |
| 4 | Few-shot Q→GraphQL example store | 🔲 Pending |
| 5 | Docs + env-var config | ✅ Done (one item outstanding) |
| — | dbt Selector Support (`--select`) | 🔲 Pending |
| — | Source Node Inclusion (`catalog.sources`) | 🔲 Pending |
| Sec-A | Identity & JWT Auth | 🟨 Trust-only shipped (signature verification pending) |
| Sec-B | RBAC + Column-Level Security | ✅ Done |
| Sec-C | Row-Level Security | 🟨 Core shipped |
| Sec-D | Data Masking | 🟨 Core shipped |
| Sec-E | Query Allow-List | 🔲 Planned |
| Sec-F | Audit Logging | 🔲 Planned |
| Sec-G | ABAC match-clauses + deny rules | 🔲 Planned |
| Sec-H | Structured row-filter DSL | 🔲 Planned |
| Sec-I | Column classifications | 🔲 Planned |
| Sec-J | External PDP (OPA / Cedar / custom) | 🔲 Planned |
| Sec-K | Hot reload of access.yml | 🔲 Planned |
| Sec-L | Policy test harness + `policy explain` CLI | 🔲 Planned |

---

## ✅ Phase 0 — dbt-colibri Delegation

| Item | Status |
|---|---|
| `dbt-colibri>=0.3.0,<0.4` dep added | ✅ |
| `prepare_scope` + `to_node` wired | ✅ |
| Single-pass `_extract_both()` for column lineage + JOIN mining | ✅ |
| `ColumnLineageEdge` deleted; typed graph edges | ✅ |
| Lineage type normalization (`pass-through` → `pass_through`) | ✅ |
| `remove_quotes`/`remove_upper` for Postgres/BigQuery dialects | ✅ |
| `source_model` → `unique_id` for cross-package disambiguation | ⚠️ skipped — still short names; deferred until multi-package use |
| `LineageSchema.format_version = "2"` | ⚠️ skipped — tied to above |
| Snapshot test against baseline `lineage.json` | ❌ not added |
| New adapter fixtures (Postgres CamelCase, BigQuery backtick, UNNEST, two-package) | ❌ not added |

---

## ✅ Phase 1 — Relationship Derivation Polish

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
| Reverse-relation fields (`@reverseRelation`) | ❌ permanently dropped — directed edges already encode this |

---

## ✅ Phase 2 — MCP Live Enrichment

| Item | Status |
|---|---|
| `EnrichmentConfig` in `config.py` | ✅ |
| `describe_table` wired to `_get_row_count` + `_get_sample_rows` | ✅ |
| Per-column `value_summary`: enum / date-range / distinct-values | ✅ |
| Budget semaphore limiting live DB queries | ✅ |
| `catalog.json` stats preferred over live `COUNT(*)` | ✅ |
| `--enrich-budget` CLI flag | 🔲 check |
| Unit tests (no-DB path returns nulls) | 🔲 check |
| Integration test (DuckDB fixture): distinct values + row_count + sample_rows | 🔲 check |
| Cache: second call issues 0 DB queries | 🔲 check |

---

## 🔲 Phase 3 — MCP SOTA Surface

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
| `suggest_examples` tool stub wired (impl in Phase 4) | 🔲 |

**Testing:**
- `get_query_syntax` response < 2KB
- `search_tables("order")` ranks `orders` and `stg_orders` first on jaffle-shop fixture
- `schema://overview` resource includes every table name

---

## 🔲 Phase 4 — Few-Shot Q→GraphQL Example Store

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

## ✅ Phase 5 — Docs + Env-Var Config

| Item | Status |
|---|---|
| `pydantic-settings>=2.0` dep | ✅ |
| `AppConfig → BaseSettings`, `env_prefix="DBT_GRAPHQL__"`, `env_nested_delimiter="__"` | ✅ |
| Precedence: init > env > file > defaults | ✅ |
| `docs/mcp.md` | ✅ |
| `docs/configuration.md` | ✅ |
| `docs/architecture.md` updates | ✅ |
| `docs/access-policy.md` | ✅ |
| `config.example.yml` at repo root (commented Helm-style defaults) | 🔲 outstanding |

---

## 🔲 dbt Selector Support (`--select` / `--exclude`)

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

## 🔲 Source Node Inclusion (`catalog.sources`)

**Motivation:** FK relationships pointing to a dbt source table are silently dropped because `extract_project` only iterates `catalog.nodes` and skips `catalog.sources`.

**Approach:** Iterate `catalog.sources` in addition to `catalog.nodes`. Create `ModelInfo` entries for source tables that are FK targets of selected models. Mark them as read-only.

**Scope:**
- Extend `extract_project` to iterate `catalog.sources`.
- Extend `build_relationships` to resolve source node unique IDs (`source.*`).
- Formatter and SQL compiler already work generically via table names — minimal changes needed.

---

## 🔲 Security & Governance

### Background

The two primary references for this design:

- **Cube.dev Access Policies** — member-level (column) access, row-level filters, data masking; declarative YAML policies evaluated per request against JWT `securityContext`; OR semantics across multiple matching roles.
- **GraphJin Production Security** — RBAC with role-table-operation bindings; compile-time row filter injection; column allowlists; production query allow-lists that prevent ad-hoc query execution.

**Design principles:**
1. **Compile-time enforcement** — row filters and masking are injected into SQL at query-compile time, not post-processed in Python. They cannot be bypassed or leaked.
2. **Declarative** — all policy lives in `access.yml` alongside `db.graphql`; no code changes per policy update.
3. **Context-driven** — JWT claims drive dynamic filtering (`$jwt.sub`, `$jwt.claims.region`).
4. **OR semantics** — if a user matches multiple roles, the most permissive applicable policy wins (additive access).

---

### Sec-A — Identity & JWT Auth 🟨 Trust-only shipped

**Design — OAuth 2.0 Resource Server.** dbt-graphql is a **Resource
Server**, not an Authorization Server. An external identity provider
(Auth0 / Keycloak / Cognito / Clerk / Cube / a custom service) issues
signed JWTs; we verify the signature, read the payload, and evaluate
policy against it. We never handle credentials, never issue tokens,
never call a login endpoint. Translation/exchange (opaque token → JWT,
session cookie → JWT, mTLS → JWT) belongs in a reverse proxy or a
sidecar service that sits in front of us — from our POV the wire
format is always `Authorization: Bearer <jwt>`.

This is the Cube.dev model (see
[Cube JWT auth docs](https://cube.dev/docs/product/auth/methods/jwt))
and the same split used by Hasura, Envoy's JWT filter, the Kubernetes
API server, and every OAuth 2.0 resource server.

**Status of shipped pieces (`77f86c2`, `ef417d6`):**
- `JWTAuthBackend` wired into Starlette's `AuthenticationMiddleware`
- PyJWT dependency
- `JWTPayload` dot-access wrapper available to `when:` and `row_level:`
- HTTP integration tests in `tests/integration/test_policy_http.py`

**Important caveat:** the current backend passes
`options={"verify_signature": False}` — it trusts whatever JWT the
client sends. Signature verification is the one item blocking a real
production story. Do not expose the API to untrusted networks until
the verification work below is done.

**Remaining work — config additions (`config.yml`):**
```yaml
security:
  jwt:
    # Option A: shared-secret HMAC (HS256/384/512)
    secret: "env:JWT_SECRET"
    # Option B: asymmetric via JWKS (RS256/ES256)
    jwks_url: "https://example.auth0.com/.well-known/jwks.json"
    algorithms: [RS256]               # allow-list of acceptable algs
    audience: "dbt-graphql"           # optional aud check
    issuer: "https://example.auth0.com/"  # optional iss check
    leeway_s: 30                      # clock-skew tolerance for exp/nbf
```

**Explicitly out of scope for Sec-A:**
- API keys — resource servers don't mint credentials. If a caller
  needs a long-lived token, they get one from the Authorization Server
  and send it as a JWT. A middleware in front of us can translate API
  keys to JWTs on the fly.
- `anonymous_role` config — "no/invalid token" is already expressible
  in policy via `when: "jwt.sub == None"`. No config wiring needed.
- Login / password / session handling — this is an Authorization
  Server concern, not a Resource Server concern.

| Item | Status |
|---|---|
| `JWTAuthBackend` + Starlette middleware (trust-only) | ✅ |
| `JWTPayload` dot-access available in `when:` / `row_level:` | ✅ |
| PyJWT dependency | ✅ |
| HTTP integration tests for policy + JWT | ✅ |
| `security.jwt` pydantic config | 🔲 |
| HMAC signature verification (HS256) | 🔲 |
| JWKS / asymmetric verification (RS256) with key caching | 🔲 |
| `exp` / `nbf` / `aud` / `iss` validation with configurable leeway | 🔲 |
| Fail-closed: reject unsigned / unknown-alg / malformed tokens with 401 | 🔲 |

---

### Sec-B — RBAC + Column-Level Security ✅ Done

**Status:** The shipped engine uses `policies[*].when` (simpleeval expressions
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
| Wildcard `"*"` table policy | ❌ dropped — use `include_all` per-table instead |
| Table-level default-deny (unlisted table → `FORBIDDEN_TABLE`) | ✅ |
| Strict columns (unauthorized column → `FORBIDDEN_COLUMN`, not silent strip) | ✅ |
| Nested-relation policy enforcement (columns / masks / row filters) | ✅ |
| Structured GraphQL error extensions (`code`, `table`, `columns`) | ✅ |
| `--policy PATH` CLI override of `config.security.policy_path` | 🔲 |

---

### Sec-C — Row-Level Security 🟨 Core shipped

**Status:** Row filters are Jinja templates rendered through a
`SandboxedEnvironment(finalize=...)` hook so every `{{ expression }}` becomes
a SQL bind parameter. Values are bound via
`text(sql).bindparams(**params)` — SQL injection via JWT claims is
structurally impossible. OR semantics across matching policies.

**Reference:** [`docs/access-policy.md`](docs/access-policy.md) §
*`row_level` template reference*.

| Item | Status |
|---|---|
| Jinja template → `:bind_param` rendering with `finalize` | ✅ |
| OR merge across matching policies (per-policy name prefix) | ✅ |
| Merge with user `where:` in `compile_query` | ✅ |
| SQL injection regression test | ✅ |
| Static-predicate passthrough (`published = TRUE`) | ✅ |
| Structured DSL alternative to raw-SQL templates | 🔲 → Sec-H |

---

### Sec-D — Data Masking 🟨 Core shipped

**Status:** Mask expressions are raw SQL fragments from `access.yml`
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
| Conflict detection (raise when policies disagree on mask SQL) | ✅ |
| Mask-by-classification (declare once, apply by tag) | 🔲 → Sec-I |
| Dialect safety: reject `;` / `--` in mask strings at load time | 🔲 |

---

### Sec-E — Query Allow-List

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

### Sec-F — Audit Logging

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

### Sec-G — ABAC `match:` clauses + deny rules

**Motivation:** Today's `when:` is an opaque Python-style string. SOTA
authz engines (OPA, Cedar, Hasura metadata) express conditions as a
**structured attribute-based** tree so policies are statically inspectable
(*"which policies could apply to this JWT?"*) and machine-testable. Also:
permissive-OR semantics cannot express "contractors never see salary, even
if they are also analysts" — deny rules with highest precedence fix that.

**Policy additions:**
```yaml
- name: analyst
  match:
    all:
      - { jwt.groups: { contains: analysts } }
      - { jwt.claims.level: { gte: 3 } }
  tables: { ... }

- name: contractor_deny
  match:
    all: [ { jwt.groups: { contains: contractors } } ]
  deny:
    customers: [salary, ssn]   # hard deny, overrides all allow rules
```

**Behavior:** `match:` coexists with the existing `when:` string for two
releases, then `when:` is deprecated. Both compile to the same
`MatchTree` AST used by the engine and by the test harness (Sec-L).

| Item | Status |
|---|---|
| `MatchTree` AST + compiler for both `when:` and `match:` | 🔲 |
| Operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `exists`, `and`, `or`, `not` | 🔲 |
| `deny:` rules (highest precedence, short-circuits OR merge) | 🔲 |
| Deprecation warning for string `when:` on load | 🔲 |

---

### Sec-H — Structured row-filter DSL

**Motivation:** Raw-SQL `row_level` templates are dialect-specific and
impossible to validate statically (typos in column names surface as
runtime errors). A Hasura-style boolean expression tree compiles to
SQLAlchemy expressions and is validated against the `TableRegistry` at
load time.

**Policy additions:**
```yaml
tables:
  customers:
    row_filter:
      all:
        - { col: org_id, eq: { jwt: claims.org_id } }
        - any:
            - { col: is_public, eq: true }
            - { col: owner_id, eq: { jwt: sub } }
```

**Behavior:** The DSL is a direct replacement for `row_level:` (both
supported during migration). Column names are resolved against the table
registry at load time — unknown columns fail policy load, not the first
request.

| Item | Status |
|---|---|
| DSL Pydantic schema (`RowFilterExpr` discriminated union) | 🔲 |
| Compiler → SQLAlchemy `BinaryExpression` / `BooleanClauseList` | 🔲 |
| Load-time column validation against `TableRegistry` | 🔲 |
| Coexistence with `row_level:` raw-SQL template | 🔲 |

---

### Sec-I — Column classifications

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

### Sec-J — External Policy Decision Point (OPA / Cedar / custom)

**Motivation:** At enterprise scale, authz decisions live in a shared PDP
(Policy Decision Point) — Open Policy Agent, AWS Cedar, or a home-grown
service — so every microservice enforces the same rules with the same
audit trail. Making `dbt-graphql` a **PEP** (Policy Enforcement Point)
that delegates to a PDP slots cleanly into that architecture. Kubernetes,
Envoy, Istio, and most large authz platforms use this exact shape.

**Config additions:**
```yaml
security:
  pdp:
    url: "http://opa:8181/v1/data/dbt_graphql/allow"
    timeout_ms: 50                      # fail closed on timeout
    cache:
      ttl_s: 5                          # per-decision local cache
      max_entries: 1000
    input_builder: default              # "default" | "custom-module:func"
```

**Decision request body (`default` builder):**
```json
{
  "input": {
    "subject":   { "sub": "u1", "groups": ["analysts"], "claims": {"org_id": 7} },
    "action":    "read",
    "resource":  { "table": "customers", "columns": ["email", "ssn"] },
    "environment": { "ip": "...", "ts": "..." }
  }
}
```

**Decision response:**
```json
{
  "result": {
    "allowed": true,
    "allowed_columns": ["customer_id", "email"],
    "masks": { "email": "CONCAT('***@', SPLIT_PART(email, '@', 2))" },
    "row_filter_sql": "org_id = :org",
    "row_filter_params": { "org": 7 }
  }
}
```

**Behavior:** When `pdp.url` is set, the built-in engine is skipped and
every request produces a PDP call (with a per-request cache keyed on
subject + resource). Load policies locally as fallback when the PDP is
unreachable *only if* explicitly enabled — otherwise fail closed.

| Item | Status |
|---|---|
| `PdpClient` (httpx) + timeout / fail-closed semantics | 🔲 |
| Input-builder contract + default implementation | 🔲 |
| Response schema + mapping to `ResolvedPolicy` | 🔲 |
| Per-request local decision cache | 🔲 |
| OPA integration test (docker compose with a minimal Rego bundle) | 🔲 |

---

### Sec-K — Hot reload of `access.yml`

**Motivation:** Role/claim changes shouldn't require a full API restart.
Watch the file, rebuild the engine, swap it atomically.

| Item | Status |
|---|---|
| `watchfiles`-based observer in the API lifespan | 🔲 |
| Atomic swap of `PolicyEngine` reference on reload | 🔲 |
| Reload-failed fallback: keep previous engine, log loud error | 🔲 |
| OTel counter `policy.reload.{success,failure}` | 🔲 |

---

### Sec-L — Policy test harness + `policy explain` CLI

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

## Open Deviations

| Item | Decision |
|---|---|
| Short names vs `unique_id` in lineage (Phase 0) | Deferred — relevant only when multi-package projects are encountered |
| Reverse relations (`@reverseRelation`) | Permanently dropped — directed edges already encode bidirectional traversal |
| Wildcard `"*"` table policy | Permanently dropped — operators must enumerate tables, or use `include_all` per-table. Wildcards make it too easy to over-grant when new tables are added. |
| Row-filter template engine | Jinja2 `SandboxedEnvironment` with `finalize=` hook. Every `{{ expression }}` becomes a SQL bind param; values never hit the rendered SQL. Chosen over (a) raw regex extraction (no conditionals/filters), (b) `jinjasql` (less active), (c) moving directly to a structured DSL (larger scope — now tracked as Sec-H). |
| `when:` evaluator | `simpleeval` — AST-based, rejects dunders + builtins, keeps the Python-flavored syntax operators already use. Chosen over (a) raw `eval()` with empty builtins (weaker), (b) CEL (different syntax, bigger dep), (c) `asteval` (comparable but less widely adopted). |
| JWT verification (Sec-A) | Shipped unverified in dev only — base64 decode. Signature verification is mandatory before `access.yml` is relied on for production data; gated on Sec-A. |
