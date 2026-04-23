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
| Sec-A | Identity & JWT Auth | 🔲 Planned |
| Sec-B | RBAC + Column-Level Security | 🔲 Planned |
| Sec-C | Row-Level Security | 🔲 Planned |
| Sec-D | Data Masking | 🔲 Planned |
| Sec-E | Query Allow-List | 🔲 Planned |
| Sec-F | Audit Logging | 🔲 Planned |

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

### Sec-A — Identity & JWT Auth

**Motivation:** Foundation for all subsequent security phases. Without a verified identity, RBAC/RLS/masking have no subject to evaluate against.

**Config additions (`config.yml`):**
```yaml
security:
  jwt:
    secret: "env:JWT_SECRET"           # HMAC-SHA256
    # OR:
    jwks_url: "https://..."            # RS256 via JWKS endpoint
    algorithm: HS256                   # HS256 | RS256
    claims_namespace: ""               # optional prefix stripped from claim keys
  api_keys:
    - key: "env:SERVICE_KEY_1"
      role: service                    # maps directly to a role name
  anonymous_role: anon                 # role assigned when no token present
```

**Files to create/modify:**
- `src/dbt_graphql/api/auth.py` — JWT decoder, API key validator, `SecurityContext` dataclass (`user_id`, `email`, `groups`, `raw_claims`)
- `src/dbt_graphql/api/app.py` — Starlette middleware injecting `request.state.security_context`
- `src/dbt_graphql/config.py` — `SecurityConfig`, `JwtConfig`, `ApiKeyConfig`

| Item | Status |
|---|---|
| `SecurityConfig` Pydantic model | 🔲 |
| JWT decode middleware (HS256 + RS256/JWKS) | 🔲 |
| API key validation | 🔲 |
| Anonymous role fallback | 🔲 |
| `SecurityContext` propagated to all resolvers | 🔲 |

---

### Sec-B — RBAC + Column-Level Security

**Motivation:** Most teams need table-level and column-level access control before row-level logic. This is the highest-value security primitive.

**Policy file (`access.yml`):**
```yaml
roles:
  - name: admin
    match_groups: ["data-admins"]   # matched against JWT groups claim
    tables:
      "*":
        allow: [read]

  - name: analyst
    match_groups: ["analysts"]
    tables:
      orders:
        allow: [read]
        columns:
          includes: ["order_id", "customer_id", "status", "created_at"]
      customers:
        allow: [read]
        columns:
          excludes: ["email", "phone", "ssn"]

  - name: anon                       # unauthenticated
    tables:
      products:
        allow: [read]
        columns:
          includes: ["product_id", "name", "price"]
```

**Behavior:**
- User's JWT `groups` claim is matched against `match_groups` → produces a set of active roles.
- Column `includes` / `excludes` is evaluated per column in the GraphQL selection; unlisted columns are stripped silently (or error in strict mode).
- Wildcard `"*"` in table name grants policy to all tables.
- `allow: [read]` is the only supported scope initially; `write` reserved for future mutations.

**Files to create/modify:**
- `src/dbt_graphql/api/policy.py` — `PolicyLoader` (Pydantic parse of `access.yml`), `RoleResolver` (JWT groups → active roles), `ColumnPermission.evaluate(table, column, roles) → allowed: bool`
- `src/dbt_graphql/api/resolvers.py` — wrap each resolver to strip disallowed columns before returning
- `src/dbt_graphql/cli.py` — `--policy PATH` flag for `serve`
- `access.example.yml`

| Item | Status |
|---|---|
| `access.yml` Pydantic schema | 🔲 |
| Role resolver (JWT groups → role set) | 🔲 |
| Column allowlist/denylist evaluation | 🔲 |
| Resolver column stripping | 🔲 |
| Table-level block (role has no policy for table → 403) | 🔲 |
| Wildcard table policy | 🔲 |
| `access.example.yml` | 🔲 |

---

### Sec-C — Row-Level Security

**Motivation:** The most impactful data isolation primitive. Users in multi-tenant systems should only see their own rows, without the GraphQL client needing to include the filter.

**Policy additions (`access.yml`):**
```yaml
roles:
  - name: regional_analyst
    match_groups: ["regional-analysts"]
    tables:
      sales:
        allow: [read]
        row_filter:
          region: { eq: "$jwt.claims.region" }
      orders:
        allow: [read]
        row_filter:
          sales_rep_id: { eq: "$jwt.sub" }
```

**Template variables:**
- `$jwt.sub` — JWT subject (user ID)
- `$jwt.email` — JWT email claim
- `$jwt.claims.<key>` — arbitrary claim from token
- `$jwt.groups[0]` — first group

**Behavior:**
- Row filters are resolved at request time by substituting JWT claim values.
- Injected into `compile_query()` as additional WHERE predicates, merged with `AND` against any user-supplied `where:` argument.
- Applied at SQL generation time — the filter appears in the SQL sent to the database; the application layer never sees unfiltered rows.

**Files to modify:**
- `src/dbt_graphql/api/policy.py` — `RowFilterEvaluator`: resolves template vars against `SecurityContext`, produces SQLAlchemy filter expression
- `src/dbt_graphql/compiler/query.py` — `compile_query(...)` accepts optional `row_filters: list[BinaryExpression]`; merges with existing WHERE

| Item | Status |
|---|---|
| Template variable resolver (`$jwt.*` → concrete value) | 🔲 |
| Row filter → SQLAlchemy expression compilation | 🔲 |
| Merge with user `where:` in `compile_query` | 🔲 |
| Multi-role filter merge (OR across roles, AND with user filters) | 🔲 |

---

### Sec-D — Data Masking

**Motivation:** Some columns should be visible in shape but not in value for non-privileged roles (e.g. show last 4 of SSN, domain-only of email). Denial is too blunt; masking enables richer analytics while protecting PII.

**Policy additions (`access.yml`):**
```yaml
roles:
  - name: analyst
    tables:
      customers:
        allow: [read]
        mask:
          email: "CONCAT('***@', SPLIT_PART(email, '@', 2))"  # SQL expression
          ssn: "CONCAT('***-**-', RIGHT(ssn, 4))"
          salary: null                                          # static NULL
          phone: "CONCAT('***-***-', RIGHT(phone, 4))"
```

**Behavior:**
- For roles without a mask rule: column selected normally.
- For roles with a mask rule: `SELECT email` replaced with `SELECT <mask_expr> AS email` in `compile_query()`.
- Static `null` mask emits `SELECT NULL AS email`.
- When a user matches multiple roles, the least-masked (most permissive) expression wins — if admin role has no mask and analyst role has a mask, admin sees raw values.

**Files to modify:**
- `src/dbt_graphql/api/policy.py` — `MaskingEvaluator`: resolves effective mask expression per column per role set
- `src/dbt_graphql/compiler/query.py` — accept `column_masks: dict[str, str]`; emit `sqlalchemy.text(mask_expr).label(column_name)` for masked columns

| Item | Status |
|---|---|
| Mask expression resolution (role set → per-column mask) | 🔲 |
| SQL mask injection in `compile_query` | 🔲 |
| `null` static mask | 🔲 |
| Multi-role mask precedence (least-masked wins) | 🔲 |
| Dialect safety: validate mask expressions don't contain `;` or `--` | 🔲 |

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

## Open Deviations

| Item | Decision |
|---|---|
| Short names vs `unique_id` in lineage (Phase 0) | Deferred — relevant only when multi-package projects are encountered |
| Reverse relations (`@reverseRelation`) | Permanently dropped — directed edges already encode bidirectional traversal |
