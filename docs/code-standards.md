# Code Standards

Coding conventions and tooling standards for the dbt-graphql project.

---

## Language & runtime

- **Python 3.11–3.13** (pinned in `pyproject.toml`: `>=3.11,<3.14`)
- Full type hints on all public functions and methods
- Pydantic v2 for configuration models and IR domain models

## Type system

- **Pydantic v2 BaseModel** — used for all configuration (`AppConfig`, `CacheConfig`, `JWTConfig`) and IR models (`ProjectInfo`, `ModelInfo`, `ColumnInfo`, `RelationshipInfo`)
- **Standard library `@dataclass`** — used for internal processor types (mutable, lightweight, no validation needed)
- **Type aliases** — used where it improves readability
- Crossing the dataclass→Pydantic boundary is a deliberate step in `pipeline.extract_project()`

## Async

- `async`/`await` throughout the serve layer
- Async database drivers: `asyncpg` (PostgreSQL), `aiomysql` (MySQL/MariaDB/Doris)
- Ariadne async resolvers
- `httpx.AsyncClient` for JWKS fetching
- `pytest-asyncio` for async test support

## Testing

- **Framework**: pytest + pytest-asyncio + pytest-docker
- **Layers**:
  - Unit tests — pure logic (cache keys, compiler, policy evaluation, row filter DSL, formatter)
  - Integration tests — PostgreSQL + MySQL parametrized with Docker fixtures
- **Docker fixtures**: `postgres:5433`, `mysql:3307`, `redis:6380` (non-standard ports to avoid host conflicts)
- **Coverage**: `pytest-cov` configured in `pyproject.toml`
- **Test data**: `tests/` directory mirrors `src/dbt_graphql/` structure

## Naming conventions

| Element                       | Convention         | Example                                          |
| ----------------------------- | ------------------ | ------------------------------------------------ |
| Functions, methods, variables | `snake_case`       | `extract_project()`, `compile_query()`           |
| Classes                       | `PascalCase`       | `ProjectInfo`, `PolicyEngine`, `DatabaseManager` |
| Constants                     | `UPPER_SNAKE_CASE` | `MAX_DEPTH`, `DEFAULT_TTL`                       |
| Modules, packages             | `snake_case`       | `row_filter.py`, `cache/`                        |
| Pydantic model fields         | `snake_case`       | `jwks_url`, `lock_safety_timeout`                |
| Config file keys              | `snake_case`       | `query_max_depth`, `mcp_enabled`                 |

## Module patterns

### Processor pattern

Each processor under `dbt/processors/` has a single responsibility corresponding to one input surface:

- `constraints.py` — dbt contract constraints
- `data_tests.py` — dbt data tests
- `compiled_sql.py` — compiled SQL analysis

Processors are independent, composable, and produce lightweight `@dataclass` types.

### IR pattern

The Intermediate Representation (`ir/models.py`) is the single boundary between extraction and all downstream consumers (formatters, compilers, MCP tools). No module downstream of the IR reads dbt artifacts directly. This makes it tractable to add alternative output formats or swap the upstream source.

### Config pattern

All configuration uses Pydantic Settings with:

1. YAML file loading (`config.yml`)
2. Environment variable override with `DBT_GRAPHQL__` prefix and `__` nested delimiter
3. Hard-coded defaults in `defaults.py`
4. Precedence: init > env > file > defaults

## Logging

- **loguru** — structured logging throughout
- **OTel intercept** — log records are bridged to OpenTelemetry log signals
- Log levels configurable via `monitoring.logs.level`

## Observability

- **OpenTelemetry** for all three signals: traces, metrics, logs
- Auto-instrumentation for SQLAlchemy, Starlette, and httpx
- Custom OTel metrics for cache outcomes, auth results, and connection wait times
- `timed()` async context manager for consistent span recording
- Exporters: OTLP over HTTP or gRPC (configurable per signal)

## SQL

- **SQLAlchemy Core** (not ORM) for all database interaction
- Correlated subqueries for nested GraphQL relations (not LATERAL joins)
- Dialect-aware JSON aggregation via `@compiles` extensions
- No raw SQL strings in the data-access path — all user-facing filters compile through SQLAlchemy expressions

## Security

- **Secure by default** — auth is on, introspection is off in production
- **Deny-wins** policy merge — deny rules always take precedence over allow rules
- **No silent column stripping** — unauthorized columns produce structured `FORBIDDEN_COLUMN` errors
- **Compile-time enforcement** — row filters, masks, and column restrictions are injected into SQL at compile time
- **No template engines in the data path** — row filter DSL compiles to SQLAlchemy `ColumnElement`, not interpolated SQL strings

## Error handling

- Structured GraphQL errors with `extensions.code` field
- Error codes: `FORBIDDEN_TABLE`, `FORBIDDEN_COLUMN`, `POOL_TIMEOUT`, `MAX_LIMIT_EXCEEDED`, `POLICY_MASK_CONFLICT`, `QUERY_TOO_DEEP`, `QUERY_TOO_COMPLEX`
- HTTP status code mapping: 503 for pool timeout, 401 for auth failure

## Dependencies

### Core dependencies

| Package                         | Purpose                                              |
| ------------------------------- | ---------------------------------------------------- |
| `ariadne`                       | GraphQL execution engine (async)                     |
| `graphql-core`                  | GraphQL AST parsing and validation                   |
| `sqlalchemy[asyncio]`           | Async database access (Core, not ORM)                |
| `sqlglot`                       | SQL parsing for lineage extraction                   |
| `dbt-artifacts-parser`          | Schema-aware dbt artifact loading                    |
| `dbt-colibri`                   | Column lineage traversal (core logic absorbed)       |
| `pydantic-settings`             | Configuration with YAML + env-var support            |
| `simpleeval`                    | Safe expression evaluation for policy `when` clauses |
| `cashews`                       | Result cache + singleflight                          |
| `joserfc`                       | JWT/JWS/JWK primitives                               |
| `fastmcp`                       | MCP server framework                                 |
| `uvicorn[standard]`             | ASGI server                                          |
| `loguru`                        | Structured logging                                   |
| `opentelemetry-sdk` + exporters | Observability (traces, metrics, logs)                |

### Optional dependencies

| Extra      | Package    | Purpose                               |
| ---------- | ---------- | ------------------------------------- |
| `postgres` | `asyncpg`  | PostgreSQL async driver               |
| `mysql`    | `aiomysql` | MySQL/MariaDB/Doris async driver      |
| `redis`    | `redis`    | Redis cache backend for multi-replica |

## Build

- **Build backend**: `uv_build` (uv's build backend)
- **Package manager**: uv
- **Entry point**: `dbt-graphql = "dbt_graphql.cli:main"`

## Linting

- **ruff** — fast Python linter and formatter
- **ty** — type checking
