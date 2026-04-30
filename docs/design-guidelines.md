# Design Guidelines

Design principles and patterns for contributors to dbt-graphql.

---

## Core principles

These are the tiebreakers. When in doubt, refer to these.

1. **The dbt project is the source of truth** — We do not ask users to author a second modeling layer. If dbt has a `relationships` test, it is an edge in the graph. If dbt has `unique`, it is a `@unique` directive. The cost of adopting dbt-graphql is approximately zero beyond "keep your dbt project clean." Consequence: dbt-graphql cannot invent metadata the dbt project doesn't have.

2. **A single format-agnostic IR** — `ProjectInfo` is the boundary between dbt and formatters. Every formatter, compiler, and MCP tool consumes `ProjectInfo` — never `manifest.json` directly. This makes it tractable to add alternative output formats or swap the upstream source without touching the rest of the code.

3. **Dataclasses for processors, Pydantic for IR** — `dbt/processors/*` produce lightweight `@dataclass` types (internal, mutable, fast). `ir/models.py` uses `BaseModel` (long-lived contract, needs validation and serialization). Crossing the boundary is a deliberate step in `pipeline.extract_project()`.

4. **Preserve the SQL type** — GraphQL field gets a standard scalar (`Int`, `Float`, `String`) for tooling compatibility; `@column(type: "NUMERIC", size: "10,2")` preserves the exact database type. The compiler never reverse-engineers SQL types from GraphQL scalars.

5. **Read-only by design** — No mutations, no writes. The target is always a `SELECT` tree. Removes an entire class of write-path risk.

6. **MCP-first for agents** — The primary consumer is an agent, not a human. The MCP server is designed around how agents actually plan: list → describe → find path → build query → execute.

7. **Cross-warehouse, not Postgres-only** — SQL emitted via SQLAlchemy Core with `@compiles` extensions for dialect-specific rewrites. No LATERAL joins (Apache Doris compatibility).

8. **Don't parse what dbt already parsed** — `dbt-artifacts-parser` owns manifest/catalog validation. `sqlglot` owns SQL parsing. `sqlalchemy` owns SQL generation. `graphql-core` owns GraphQL parsing. `ariadne` owns execution. dbt-graphql is the glue.

---

## The IR boundary

The Intermediate Representation is the most important architectural constraint.

```
  dbt artifacts  →  processors  →  ProjectInfo (IR)  →  formatters / compiler / MCP
  ──────────────────────────────────────────────────────────────────────────────────
                    LEFT OF IR                        RIGHT OF IR
```

**Rules:**

- Processors (left of IR) may read dbt artifacts and produce `@dataclass` types.
- `pipeline.extract_project()` converts processor types into Pydantic IR types.
- Everything right of the IR (`formatter/`, `compiler/`, `graphql/`, `mcp/`) consumes `ProjectInfo` — never raw artifacts.
- When adding a new output format, write a new consumer of `ProjectInfo`. Do not modify processors.

---

## Processor pattern

Each module under `dbt/processors/` corresponds to one input surface:

| Processor         | Input                    | Output                                 |
| ----------------- | ------------------------ | -------------------------------------- |
| `constraints.py`  | dbt contract constraints | PK/FK `RelationshipInfo`               |
| `data_tests.py`   | dbt data tests           | Enums + FK `RelationshipInfo`          |
| `compiled_sql.py` | Compiled SQL via sqlglot | Column lineage + FK `RelationshipInfo` |

**Guidelines:**

- Processors are independent — no processor depends on another's output.
- Each processor produces lightweight `@dataclass` types, not Pydantic models.
- Relationship merging with priority ordering happens in `pipeline.py`, not in processors.
- When adding a new relationship source, create a new processor file.

---

## Config pattern

All configuration follows the same pattern:

```
defaults.py  →  config.yml  →  DBT_GRAPHQL__* env vars  →  init arguments
(lowest)                                              (highest)
```

**Guidelines:**

- New config fields go into the appropriate Pydantic Settings model.
- Hard-coded defaults live in `defaults.py`, not in the Pydantic model.
- Every config field must be overridable via environment variable (automatic with Pydantic Settings).
- Document new config fields in `config.example.yml` and `docs/configuration.md`.

---

## Testing patterns

### Unit tests

- Test pure logic in isolation: cache keys, compiler output, policy evaluation, row filter DSL, formatter output.
- Mock external dependencies (database connections, HTTP calls).
- No Docker required.

### Integration tests

- Use `pytest-docker` for PostgreSQL, MySQL, and Redis fixtures.
- Non-standard ports (5433, 3307, 6380) to avoid host conflicts.
- Parametrize across databases where behavior differs.

### Test structure

```
tests/
├── test_cache/           # Cache key derivation, TTL, singleflight
├── test_compiler/        # SQL compilation, dialect-specific output
├── test_dbt/             # Artifact loading, processor output
├── test_formatter/       # SDL generation, round-trip parsing
├── test_graphql/         # Auth, guards, policy, resolvers, row filter
├── test_mcp/             # MCP tools, discovery, enrichment
├── test_cli/             # CLI argument parsing
└── test_integration/     # Full-stack HTTP tests with Docker
```

**Guidelines:**

- Every new feature needs both unit and integration tests.
- Policy tests must cover: allowed access, denied access, cross-cutting deny precedence, mask conflicts, missing JWT claims.
- Compiler tests must cover each supported database dialect.

---

## Security principles

1. **Secure by default** — `dev_mode: false` requires JWT config and disables introspection. The server fails to start if security is misconfigured.

2. **Deny-wins** — Any matching deny rule takes precedence over all matching allow rules. This makes cross-cutting prohibitions ("contractors never see PII") trivially correct.

3. **No silent column stripping** — Unauthorized columns produce structured `FORBIDDEN_COLUMN` errors, not empty values. Clients always know when they're being restricted.

4. **Compile-time enforcement** — Row filters, masks, and column restrictions are injected into SQL at compile time. They cannot be bypassed at runtime.

5. **No template engines in the data path** — Row filter DSL compiles to SQLAlchemy `ColumnElement` expressions. Values bind as parameters, never interpolated into SQL strings.

6. **Fail-closed on auth errors** — Invalid or missing JWT returns HTTP 401 with `WWW-Authenticate: Bearer` header per RFC 6750.

---

## Error handling

GraphQL errors use structured extensions:

```json
{
  "errors": [
    {
      "message": "Table 'orders' is not accessible",
      "extensions": {
        "code": "FORBIDDEN_TABLE",
        "table": "orders"
      }
    }
  ]
}
```

**Standard error codes:**

| Code                   | HTTP status | Meaning                                             |
| ---------------------- | ----------- | --------------------------------------------------- |
| `FORBIDDEN_TABLE`      | 200         | Table not covered by any matching allow policy      |
| `FORBIDDEN_COLUMN`     | 200         | Column not in allowed set                           |
| `POOL_TIMEOUT`         | 503         | Connection pool checkout timed out                  |
| `MAX_LIMIT_EXCEEDED`   | 200         | `limit`/`first` exceeds `query_max_limit`           |
| `POLICY_MASK_CONFLICT` | 200         | Two matching policies disagree on a mask expression |
| `QUERY_TOO_DEEP`       | 200         | Selection depth exceeds `query_max_depth`           |
| `QUERY_TOO_COMPLEX`    | 200         | Total field count exceeds `query_max_fields`        |

**Guidelines:**

- New error conditions must use a unique `extensions.code`.
- Error messages are human-readable; `extensions.code` is machine-readable.
- Pool timeout is the only error that maps to a non-200 HTTP status (503 + `Retry-After`).

---

## Detailed references

| Document                                    | Content                                                     |
| ------------------------------------------- | ----------------------------------------------------------- |
| [Architecture & Design](architecture.md)    | Full design principles, pipeline flow, landscape comparison |
| [Code Standards](code-standards.md)         | Language, tooling, and naming conventions                   |
| [Access Policy](access-policy.md)           | Policy DSL, evaluation rules, error responses               |
| [Security](security.md)                     | JWT verification, threat model, key management              |
| [Configuration Reference](configuration.md) | Full config surface with env-var precedence                 |
