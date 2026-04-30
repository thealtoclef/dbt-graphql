# Codebase Summary

Source layout for `dbt-graphql` v0.1.0. The package lives under `src/dbt_graphql/`.

```
src/dbt_graphql/
├── __init__.py              # Public API: extract_project(), format_graphql()
├── cli.py                   # CLI entry point (--config, --output)
├── config.py                # Pydantic Settings (AppConfig) with env-var overrides
├── defaults.py              # Hard-coded runtime constants (pool sizes, TTLs, limits)
├── monitoring.py            # OTel SDK bootstrap, tracer/provider setup, timed() helper
├── pipeline.py              # Orchestration: artifacts → 3 processors → ProjectInfo IR
│
├── cache/                   # Result cache + singleflight between HTTP and warehouse
│   ├── __init__.py
│   ├── config.py            # CacheConfig Pydantic model (url, ttl, lock_safety_timeout)
│   ├── keys.py              # SHA-256 cache key derivation from SQL + bound params
│   ├── result.py            # execute_with_cache() — TTL cache + singleflight lock
│   ├── setup.py             # Lifespan hooks for cache init/shutdown
│   └── stats.py             # Hit/miss/coalesce counters, OTel counter emission
│
├── compiler/                # GraphQL → SQL compilation
│   ├── __init__.py
│   ├── connection.py        # DatabaseManager — async SQLAlchemy engine + pool
│   └── query.py             # compile_query() — correlated subqueries with JSON aggregation
│
├── dbt/                     # dbt artifact loading & processing
│   ├── __init__.py
│   ├── artifacts.py         # load_catalog(), load_manifest() — schema-aware readers
│   └── processors/          # Three independent processors, one per input surface
│       ├── __init__.py
│       ├── compiled_sql.py  # sqlglot lineage extraction + JOIN mining (dbt-colibri core)
│       ├── constraints.py   # PK/FK extraction from dbt v1.5+ contracts
│       └── data_tests.py    # Enum detection + FK relationships from dbt tests
│
├── formatter/               # IR → GraphQL SDL conversion
│   ├── __init__.py
│   ├── graphql.py           # build_registry() — ProjectInfo → TableRegistry;
│   │                        # format_graphql() — TableRegistry → SDL string
│   └── schema.py            # parse_db_graphql() — SDL → TableRegistry (round-trip)
│
├── graphql/                 # GraphQL serving layer
│   ├── __init__.py
│   ├── app.py               # GraphQLBundle — ASGI sub-app (schema + context + error handler)
│   ├── resolvers.py         # Per-table async resolvers (query → compile → execute)
│   ├── guards.py            # Depth, field count, and limit validation rules
│   ├── policy.py            # PolicyEngine — RBAC evaluation (allow/deny, masks, row filters)
│   ├── row_filter.py        # Hasura-style DSL → SQLAlchemy ColumnElement compiler
│   ├── monitoring.py        # OTel metrics + HTTP request handler instrumentation
│   └── auth/                # JWT verification (OAuth 2.0 Resource Server)
│       ├── __init__.py
│       ├── backend.py       # Starlette AuthenticationBackend integration
│       ├── keys.py          # JWKS resolver + static key resolvers (env, file, URL)
│       └── verifier.py      # JWT signature verification + claims validation
│
├── ir/                      # Intermediate Representation
│   ├── __init__.py
│   └── models.py            # Pydantic domain models:
│                            #   ProjectInfo, ModelInfo, ColumnInfo, RelationshipInfo
│
├── mcp/                     # MCP server for LLM agents
│   ├── __init__.py
│   ├── server.py            # FastMCP server — 7 tools (list, describe, find_path, etc.)
│   ├── discovery.py         # SchemaDiscovery — table search, BFS join-path finding,
│   │                        #   live enrichment (row counts, sample rows, value summaries)
│   └── usage_guide.md       # Internal usage guide for MCP tool responses
│
└── serve/                   # Server assembly
    ├── __init__.py          # run() — uvicorn entry point
    └── app.py               # create_app() — Starlette composition (lifespan, middleware)
```

## Module descriptions

### Top-level modules

| Module          | Role                                                                                                                                                       |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `__init__.py`   | Public API surface. Exposes `extract_project()` and `format_graphql()` for library usage.                                                                  |
| `cli.py`        | CLI entry point. Parses `--config` and `--output` flags; delegates to generate or serve mode.                                                              |
| `config.py`     | Pydantic Settings model (`AppConfig`) with YAML file loading and `DBT_GRAPHQL__*` env-var override support.                                                |
| `defaults.py`   | Centralized hard-coded constants — pool sizes, cache TTLs, query guard defaults, OTel service name.                                                        |
| `monitoring.py` | OpenTelemetry SDK bootstrap. Creates tracer provider, configures exporters, provides a `timed()` async context manager for span recording.                 |
| `pipeline.py`   | Orchestration layer. Loads dbt artifacts, runs three independent processors, merges relationships with priority ordering, and produces a `ProjectInfo` IR. |

### `cache/` — Result cache + singleflight

| Module      | Role                                                                                                       |
| ----------- | ---------------------------------------------------------------------------------------------------------- |
| `config.py` | `CacheConfig` Pydantic model defining cache URL, TTL, and lock safety timeout.                             |
| `keys.py`   | Derives deterministic SHA-256 cache keys from rendered SQL and bound parameter values.                     |
| `result.py` | `execute_with_cache()` — wraps resolver execution with TTL-based caching and singleflight lock coalescing. |
| `setup.py`  | Lifespan hooks that initialize and shut down the cache backend (memory or Redis).                          |
| `stats.py`  | OTel counter for cache outcomes (hit, miss, coalesced) with per-outcome attributes.                        |

### `compiler/` — GraphQL → SQL

| Module          | Role                                                                                                                                                                               |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `connection.py` | `DatabaseManager` — manages async SQLAlchemy engine creation, pool sizing, and connection lifecycle.                                                                               |
| `query.py`      | `compile_query()` — translates GraphQL selection sets into dialect-aware SQL using correlated subqueries and JSON aggregation (JSONB_AGG for PostgreSQL, JSON_ARRAYAGG for MySQL). |

### `dbt/` — dbt artifact loading & processing

| Module                       | Role                                                                                                                                               |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `artifacts.py`               | Loads and validates `catalog.json` and `manifest.json` using `dbt-artifacts-parser`. Supports dbt schema versions v1–v12.                          |
| `processors/compiled_sql.py` | Extracts column lineage and discovers FK relationships from compiled SQL using sqlglot AST analysis (absorbed dbt-colibri core logic).             |
| `processors/constraints.py`  | Extracts primary key and foreign key relationships from dbt v1.5+ contract constraints using sqlglot-based FK parsing.                             |
| `processors/data_tests.py`   | Detects enums from `accepted_values` tests and FK relationships from `relationships` tests. Reads `meta.relationship_name` and `meta.description`. |

### `formatter/` — IR → GraphQL SDL

| Module       | Role                                                                                                                                                        |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `graphql.py` | `build_registry()` converts `ProjectInfo` to a `TableRegistry` (Python schema objects). `format_graphql()` serializes the registry to a GraphQL SDL string. |
| `schema.py`  | `parse_db_graphql()` parses an existing GraphQL SDL file back into a `TableRegistry`, enabling round-trip workflow.                                         |

### `graphql/` — GraphQL serving layer

| Module             | Role                                                                                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`           | `GraphQLBundle` — Ariadne-based ASGI sub-app that wires together schema, context, and error handling.                                                 |
| `resolvers.py`     | Per-table async resolvers. Each resolver calls `compile_query()` and executes the resulting SQL against the warehouse.                                |
| `guards.py`        | Validation rules that reject queries exceeding `query_max_depth`, `query_max_fields`, or `query_max_limit`.                                           |
| `policy.py`        | `PolicyEngine` — evaluates RBAC policies against JWT claims. Produces `ResolvedPolicy` with allowed columns, masks, row filters, and blocked columns. |
| `row_filter.py`    | Compiles the Hasura-style row filter DSL (from YAML policy config) into SQLAlchemy `ColumnElement` clauses for injection into SQL WHERE clauses.      |
| `monitoring.py`    | OTel metrics collection and HTTP handler instrumentation for the GraphQL layer.                                                                       |
| `auth/backend.py`  | Starlette `AuthenticationBackend` integration. Extracts Bearer token and delegates to the verifier.                                                   |
| `auth/keys.py`     | Key resolution strategies: `JWKSResolver` (rotating key set via httpx) and `StaticKeyResolver` (env var, file, URL).                                  |
| `auth/verifier.py` | JWT signature verification and claims validation (`exp`, `nbf`, `aud`, `iss`, `required_claims`) using joserfc.                                       |

### `ir/` — Intermediate Representation

| Module      | Role                                                                                                                                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `models.py` | Pydantic domain models that form the contract between extraction and all downstream consumers: `ProjectInfo`, `ModelInfo`, `ColumnInfo`, `RelationshipInfo`. |

### `mcp/` — MCP server for LLM agents

| Module         | Role                                                                                                                                                                   |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server.py`    | FastMCP server registration with 7 tools: `list_tables`, `describe_table`, `find_path`, `explore_relationships`, `trace_column_lineage`, `build_query`, `run_graphql`. |
| `discovery.py` | `SchemaDiscovery` — BFS-based join path finding, table search, and live DB enrichment (row counts, sample rows, value summaries with configurable budget).             |

### `serve/` — Server assembly

| Module        | Role                                                                                                                              |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `__init__.py` | `run()` — uvicorn entry point that starts the Starlette app.                                                                      |
| `app.py`      | `create_app()` — assembles the Starlette application with lifespan hooks, middleware stack, and mounted sub-apps (GraphQL + MCP). |
