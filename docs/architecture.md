# Architecture & Design

This document is the source-level design doc for dbt-graphql. It covers the pipeline, the intermediate representation, each component's responsibility, the design choices behind them, and the prior-art projects that influenced those choices.

If you're looking for the landscape comparison (where dbt-graphql sits vs. Cube, Wren, Hasura, PostGraphile, …) see [comparison.md](comparison.md).

---

## Table of contents

- [1. The problem and the shape of the solution](#1-the-problem-and-the-shape-of-the-solution)
- [2. Pipeline flow](#2-pipeline-flow)
- [3. Design principles](#3-design-principles)
- [4. Intermediate representation (`ir/models.py`)](#4-intermediate-representation-irmodelspy)
- [5. dbt extraction (`dbt/`)](#5-dbt-extraction-dbt)
- [6. Lineage extraction](#6-lineage-extraction)
- [7. GraphQL schema emission (`formatter/graphql.py`)](#7-graphql-schema-emission-formattergraphqlpy)
- [8. SDL parsing & `TableRegistry` (`formatter/schema.py`)](#8-sdl-parsing--tableregistry-formatterschemapy)
- [9. GraphQL → SQL compiler (`compiler/`)](#9-graphql--sql-compiler-compiler)
- [10. Serve layer (`serve/`)](#10-serve-layer-serve)
- [11. MCP server (`mcp/`)](#11-mcp-server-mcp)
- [12. CLI (`cli.py`)](#12-cli-clipy)
- [13. Cross-cutting design notes](#13-cross-cutting-design-notes)
- [14. Prior art & inspirations](#14-prior-art--inspirations)

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
2. Emit a typed GraphQL SDL that preserves the dbt semantics via custom directives.
3. Compile GraphQL queries against that SDL into warehouse SQL.
4. Expose the same SDL and an operational surface to LLM agents via MCP.

Steps 2–4 never touch `manifest.json` again. The only coupling to dbt lives in step 1.

---

## 2. Pipeline flow

```
 dbt artifacts                     IR (ProjectInfo)            Outputs
 ─────────────                     ──────────────────           ───────
 catalog.json                                                   db.graphql      ◀── formatter/graphql.py
 manifest.json   ──▶ pipeline.extract_project()  ──▶  ───────▶  lineage.json   ◀── ProjectInfo.build_lineage_schema()
                            │                                   GraphQL API     ◀── serve/  (uses db.graphql)
                            │                                   MCP tools       ◀── mcp/    (uses ProjectInfo)
                            ▼
                     dbt/processors/
                       artifacts.py       — load manifest & catalog
                       constraints.py     — dbt v1.5+ constraints   → PKs + FKs
                       data_tests.py      — dbt data tests          → not_null / unique / enums / FKs
                       compiled_sql.py    — sqlglot over compiled SQL → table + column lineage + JOIN-derived FKs
```

Entry point: [`src/dbt_graphql/pipeline.py:22`](../src/dbt_graphql/pipeline.py) — `extract_project()`.

End-to-end steps (matching the source):

1. **Load artifacts.** `load_catalog()` / `load_manifest()` wrap `dbt_artifacts_parser.parser`. Returns typed nodes; no schema validation beyond what the parser provides.
2. **Read project metadata.** `project_name`, `adapter_type` from `manifest.metadata` — used to label the lineage output and to feed dialect-aware SQL generation.
3. **Preprocess tests.** Scans `manifest.nodes` for `not_null`, `unique`, `accepted_values` tests. Produces a `TestsResult` keyed by `"{unique_id}.{column_name}"`. Enum definitions are deduplicated by sorted value tuple so two columns with the same `accepted_values` list reuse one enum (name collisions get a numeric suffix).
4. **Extract constraints.** Reads dbt v1.5+ `constraints` on models (table-level PK / FK) and on columns (column-level). Supports both the legacy `expression="other_table(col)"` form and the v1.9+ `to="db.schema.other_table"` + `to_columns=[...]` form.
5. **Build models.** For every `model.*` node in the catalog:
   - strip SQL quoting off column names,
   - attach raw SQL types, `not_null`, `unique`, and `enum_values` from the tests result,
   - pull descriptions from the manifest,
   - sort columns by catalog index then name (stable, human-friendly order),
   - fall back `database → schema` for adapters that don't populate database (MySQL).
6. **Merge relationships.** Three sources in priority order: dbt v1.5+ `constraints` > `relationships` tests > JOIN-ON mining (sqlglot). Each `ProcessorRelationship` carries an `origin` tag (`constraint` | `data_test` | `lineage` — a `RelationshipOrigin` StrEnum) that propagates to the IR so downstream consumers can reason about confidence. Deduplication is by relationship name: whichever tier lands first wins.
7. **Attach relationships to models.** Each `RelationshipInfo` ends up on both the from-model and the to-model so downstream consumers can navigate edges without rebuilding an index.
8. **Extract lineage.** Table lineage from `depends_on.nodes` for every model. Column lineage is built with sqlglot directly — no optional-dependency gate.
9. **Build enums dict.** Flattens `{enum_name: [values]}` for formatters that want it.
10. **Return `ProjectInfo`.** The single boundary between extraction and everything that follows.

Once `ProjectInfo` is in hand, the rest of the system is deterministic: format → SDL, parse SDL → registry, compile GraphQL selections → SQL, execute.

---

## 3. Design principles

A few opinions shaped the code. When in doubt, these are the tiebreakers.

### 3.1 The dbt project is the source of truth

We do not ask users to author a second modeling layer. If dbt has a `relationships` test, it is an edge in the graph. If dbt has a `unique` test, it is a `@unique` directive. If dbt has `primary_key` constraints, it is `@id`. The cost of adopting dbt-graphql is approximately zero beyond "keep your dbt project clean" — which your analytics team already does.

Consequence: dbt-graphql **cannot** invent metadata the dbt project doesn't have. Missing descriptions stay missing. Missing relationships stay missing. That's deliberate: the tool's correctness is bounded by the quality of your dbt project, and vice versa.

### 3.2 A single format-agnostic IR

`ProjectInfo` is the boundary between dbt and formatters. Every formatter, compiler, and MCP tool consumes `ProjectInfo` — never `manifest.json` directly. This is what makes it tractable to add alternative output formats (OpenAPI, JSON Schema, Malloy, …) or swap the upstream source (e.g., a non-dbt project) without touching the rest of the code.

### 3.3 Dataclasses for processors, Pydantic for IR

- `dbt/processors/*` produce lightweight `@dataclass` types (`ProcessorRelationship`, `EnumDefinition`, …) — they're internal, mutable, and fast.
- `ir/models.py` uses `BaseModel` — it's the long-lived contract, needs validation, serialization, and aliasing (e.g., `schema_` ↔ `schema`).

Crossing the boundary is a deliberate step: `pipeline.extract_project()` converts processor types into Pydantic IR types. Nothing upstream of `ProjectInfo` can accidentally leak into a formatter.

### 3.4 Preserve the SQL type

GraphQL type systems are coarse. SQL type systems are precise (`NUMERIC(10,2)` matters). We pick both: the GraphQL field gets a PascalCase name (`Numeric`, `Varchar`, `TimestampWithTimeZone`) for tooling compatibility, and an `@sql(type: "NUMERIC", size: "10,2")` directive preserves the exact database type for the compiler. The compiler never has to parse a PascalCase GraphQL name back into SQL.

### 3.5 Read-only by design

No mutations, no writes, no upserts. The target is always a `SELECT` tree. That removes an entire class of write-path risk (row-level security, cascades, transaction semantics) and simplifies the compiler. If you need a write-heavy GraphQL backend, use Hasura or PostGraphile — they were built for it.

### 3.6 MCP-first for agents

The primary consumer of dbt-graphql is **an agent**, not a human writing GraphQL in a playground. The MCP server is not a port of the HTTP API — it's a distinct surface designed around how agents actually plan: first list, then describe, then find a join path, then build a query, then execute. Each MCP tool returns a `_meta.next_steps` field guiding the next call.

### 3.7 Cross-warehouse, not Postgres-only

SQL is emitted via SQLAlchemy Core so we get dialect-aware rendering for free. Where SQLAlchemy doesn't cover it (JSON aggregation varies wildly per engine), we use the `@compiles` extension to register per-dialect rewrites. No LATERAL joins (Apache Doris doesn't support them).

### 3.8 Don't parse what dbt already parsed

`dbt_artifacts_parser` owns manifest/catalog schema validation. `sqlglot` owns SQL parsing/qualification for column and join lineage. `sqlalchemy` owns dialect SQL generation. `graphql-core` owns GraphQL parsing. `ariadne` owns execution. dbt-graphql is the glue — small, opinionated, replaceable.

---

## 4. Intermediate representation (`ir/models.py`)

[`src/dbt_graphql/ir/models.py`](../src/dbt_graphql/ir/models.py)

Everything between extraction and the final outputs speaks IR.

### Project / model / column / relationship

Four core Pydantic models — `ColumnInfo`, `ModelInfo`, `RelationshipInfo`, and `ProjectInfo` — carry the full dbt semantics into the rest of the system. See [`src/dbt_graphql/ir/models.py`](../src/dbt_graphql/ir/models.py) for the canonical field list; what follows are the non-obvious design decisions.

Two deliberate choices here:

- **`primary_keys` lives on the model, not on columns.** In SQL a primary key is a *table-level* constraint. A column that participates in a composite PK isn't independently unique. Storing PK membership per-column would duplicate information and invite drift. When the formatter needs to decide whether a column gets `@id`, it asks `len(model.primary_keys) == 1 and col.name in model.primary_keys` — the `len == 1` check is O(1) and short-circuits the O(n) `in` check.
- **`type` is required, no default.** An empty `type` produces a broken SDL. Required-at-model-construction surfaces the problem at the source rather than in the output.

### Lineage

`LineageType` is a `StrEnum` with values `pass_through`, `rename`, `transformation`, `filter`, `join`, `unknown`. The lineage schema (`TableLineageItem`, `ColumnLineageItem`, `LineageSchema`) is defined alongside the other IR types in the same file.

`ProjectInfo.build_lineage_schema()` groups raw column-lineage dicts by `(source, target)`, constructs typed `Column` entries, and validates via Pydantic. JSON serialization uses `by_alias=True` so the output is camelCase (`sourceColumn`, `lineageType`) — friendlier to downstream JavaScript tooling.

---

## 5. dbt extraction (`dbt/`)

Three source-based processors, each responsible for one input surface. The module name tells you where the data came from, not what shape it has.

### 5.1 `dbt/artifacts.py`

Thin wrapper around `dbt_artifacts_parser.parser`. Returns typed manifest/catalog objects. The only rule: if someone wants to swap the upstream source (e.g., a dbt Cloud API), this is the only file they need to change.

### 5.2 `dbt/processors/constraints.py` — dbt v1.5+ constraints

Extracts `constraints` on models and columns.

- **Primary keys**: both `constraints[].type == "primary_key"` on the model *and* column-level constraints. Results merged, deduplicated by unique_id.
- **Foreign keys, legacy form** (pre-v1.9): `expression="other_table(other_col)"`, `columns=["my_col"]`. Parsed with a regex in `_parse_fk_expression()`.
- **Foreign keys, modern form** (dbt v1.9+): `to="db.schema.other_table"`, `to_columns=["other_col"]`, `columns=["my_col"]`. Resolved to a model name via `_resolve_to_model()` which searches manifest nodes by `relation_name`.

Output is a list of `ProcessorRelationship` (dataclass) with `join_type` defaulting to `many_to_one` and `origin=RelationshipOrigin.constraint`. Cardinality refinement happens later, in `pipeline._rel_to_domain()`.

### 5.3 `dbt/processors/data_tests.py` — dbt data tests

Extracts everything authored as a dbt **data test** (schema test). Not to be confused with dbt *unit tests*, a newer feature for asserting model logic — those are ignored here.

- `not_null` / `unique` → booleans keyed by `"{unique_id}.{column_name}"`.
- `accepted_values` → enum definitions. Enum name is derived from the column name via `_sanitize_enum_name()` (non-alphanumerics dropped, leading digit gets a `_` prefix). Deduplication is by **sorted value tuple** — two columns with the same `accepted_values` list share one enum. Name collisions between distinct value sets get a numeric suffix.
- `relationships` → `ProcessorRelationship` objects (origin `"data_test"`). For each test node: `attached_node` → source unique_id, `column_name` → source column, `refs[0].name` → target model, `test_metadata.kwargs["field"]` → target column. Column names are cleaned through `_clean_col()` (strips `"` and `` ` ``).

Everything in this module is keyed off `node.test_metadata.name`, so adding support for a new data test type is localized here.

### 5.4 `dbt/processors/compiled_sql.py` — sqlglot over compiled SQL

Centralized sqlglot-driven extraction. Three public functions, one shared parsing pipeline living in the same file (`qualify_model_sql`, `build_schema_for_model`, `build_table_lookup`, `detect_dialect`, `sanitize_sql`, `resolve_table_to_model`):

- `extract_table_lineage(manifest)` — table edges from `depends_on.nodes`. No SQL parsing needed, but it lives here because "what came from the compiled DAG" is the conceptual fit.
- `extract_column_lineage(manifest, catalog)` — column-level lineage. See §6.2.
- `extract_join_relationships(manifest, catalog)` — FK-style relationships mined from JOIN ON clauses. For each model's compiled SQL, walks every nested scope and extracts equality pairs from each JOIN ON. Each column reference is resolved through CTE/subquery scopes to a leaf table, which is mapped to a dbt model name via the shared `table_lookup`. Direction rule: the dbt model being processed is always the `from_model`. JOINs where neither side resolves to the current model are skipped (they're upstream-to-upstream and don't describe the current model's edges); self-joins are also skipped. Emitted with `origin=RelationshipOrigin.lineage` and the lowest priority in the merge step, so an explicit constraint or `relationships` test on the same pair always wins.

### 5.5 Relationship cardinality inference

Done in `pipeline._infer_join_type()`:

| `from` column unique | `to` column unique | join_type        |
|----------------------|--------------------|------------------|
| yes                  | yes                | `one_to_one`     |
| yes                  | no                 | `one_to_many`    |
| no                   | yes                | `many_to_one`    |
| no                   | no                 | `many_to_one` *  |

\* Fallback. When uniqueness is unknown on both sides, we assume the standard FK pattern — a `relationships` test is conceptually a lookup of the "to" side, so the to-column is implicitly unique even if we don't have a test to prove it.

A column is "unique" if it has a `unique` test or is a single-column PK.

---

## 6. Lineage extraction

[`src/dbt_graphql/dbt/processors/compiled_sql.py`](../src/dbt_graphql/dbt/processors/compiled_sql.py)

### 6.1 Table-level

For every `model.*` node, emit an edge for each entry in `depends_on.nodes` that starts with `model.`, `seed.`, or `source.`. The result is `{target_model: [upstream_models]}`. Trivial and deterministic — no heuristics, no SQL parsing.

### 6.2 Column-level (via sqlglot)

Column lineage is built directly on sqlglot. For every materialized model:

1. Build a per-model schema dict `{database: {schema: {table: {col: type}}}}` restricted to `depends_on.nodes` (more performant; avoids `SELECT *` expansion against unrelated tables).
2. Sanitize the compiled SQL (dialect-specific: Oracle `LISTAGG DISTINCT` / `ON OVERFLOW` stripping).
3. Parse with the detected sqlglot dialect (adapter_type `"sqlserver"` → `"tsql"`; Postgres strips quoted identifiers so `SELECT *` can expand; BigQuery lowercases quoted identifiers).
4. `qualify()` with `validate_qualify_columns=False` and `identify=False` — the goal is scope construction, not a validated rewrite.
5. `build_scope()` → recursively trace each outer select through CTE/subquery scopes to leaf `exp.Table` nodes, classifying each hop (`pass_through` / `rename` / `transformation`) and taking the max rank across the chain.

Returns `{model: {column: [ColumnLineageEdge]}}` where each edge carries `source_model`, `source_column`, `target_column`, and `lineage_type`. The same qualify/scope pipeline feeds `extract_join_relationships` (§5.4) — both public extractors share internal helpers in the same `compiled_sql.py` module.

### 6.3 Why lineage is first-class here

No DB-introspection tool (Hasura, pg_graphql, PostGraphile) can produce lineage — lineage doesn't exist in `pg_catalog`. It only exists in the transformation graph, which is exactly what dbt owns. Exposing it alongside the schema means an LLM agent can ask not just "what columns are on this model" but "where does this column come from" — a qualitatively different capability.

---

## 7. GraphQL schema emission (`formatter/graphql.py`)

[`src/dbt_graphql/formatter/graphql.py`](../src/dbt_graphql/formatter/graphql.py)

Converts `ProjectInfo` to SDL. One `ModelInfo` becomes one `type` block.

### 7.1 Type-level directives

```graphql
type orders @database(name: mydb) @schema(name: public) @table(name: orders) {
  ...
}
```

- `@database(name:)` — warehouse database from catalog metadata
- `@schema(name:)` — warehouse schema
- `@table(name:)` — physical table name (honors the dbt `alias` if set; otherwise the model name)

Carrying these on the GraphQL type means the SQL compiler doesn't need to look the model up in a separate manifest at query time — the SDL alone is sufficient.

### 7.2 Field-level directives

For each column:

- `@sql(type: "...", size: "...")` — **always present.** Preserves the raw SQL type and any size/precision. This is the bridge between GraphQL's coarse type system and real warehouse types.
- `@id` — only on a *sole-column* primary key. Composite PK parts do not get `@id` because none of them individually identifies a row.
- `@unique` — column has a `unique` test and is not already the sole PK.
- `@relation(type: TargetModel, field: target_col)` — foreign key, rendered by looking up `(model.name, col.name)` in a precomputed `rel_map`.

### 7.3 Type mapping

`_parse_sql_type()` turns raw SQL types into `(base, size, is_array)`:

| Raw SQL                    | base                      | size    | is_array |
|----------------------------|---------------------------|---------|----------|
| `INTEGER`                  | `INTEGER`                 | ``      | false    |
| `VARCHAR(255)`             | `VARCHAR`                 | `255`   | false    |
| `NUMERIC(10,2)`            | `NUMERIC`                 | `10,2`  | false    |
| `DOUBLE PRECISION`         | `DOUBLE PRECISION`        | ``      | false    |
| `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP WITH TIME ZONE`| ``      | false    |
| `TEXT[]` (Postgres)        | `TEXT`                    | ``      | true     |
| `ARRAY<STRING>` (BigQuery) | `STRING`                  | ``      | true     |

The base name is then converted to PascalCase (`capwords()` with `_` → space, then spaces dropped) to produce a valid GraphQL name (`Integer`, `Varchar`, `DoublePrecision`, `TimestampWithTimeZone`, `Text`, `String`).

### 7.4 What is *not* emitted

- No scalar definitions. The PascalCase names are emitted as-is; the serve layer declares them as scalars when it assembles the Ariadne schema. This keeps `db.graphql` minimal and human-readable.
- No query root. `db.graphql` is a description of the warehouse, not a queryable GraphQL schema. The query root is generated at serve time.

---

## 8. SDL parsing & `TableRegistry` (`formatter/schema.py`)

[`src/dbt_graphql/formatter/schema.py`](../src/dbt_graphql/formatter/schema.py)

At serve time and at compile time, we re-parse `db.graphql` into typed Python objects (`ColumnDef`, `TableDef`) — the inverse of the formatter. See [`src/dbt_graphql/formatter/schema.py`](../src/dbt_graphql/formatter/schema.py) for the field definitions.

`TableRegistry` is a dict-like wrapper: `registry[name]`, `name in registry`, `iter(registry)`. The compiler and the MCP layer both look up tables through it.

Parsing uses `graphql-core`. `_unwrap_type()` walks `NonNullTypeNode` → `ListTypeNode` → `NamedTypeNode` to compute `(type_name, not_null, is_array)`. `_directive_args()` flattens directive arguments into a `dict[str, str]`.

**Why parse, instead of keeping the IR around?** Because `db.graphql` is a deployable artifact. In production you generate it once (at CI time, against CI dbt artifacts) and ship it. The serve layer needs only the SDL, not the whole dbt project — so it can run in containers that don't have `manifest.json`.

---

## 9. GraphQL → SQL compiler (`compiler/`)

The most interesting part. [`src/dbt_graphql/compiler/query.py`](../src/dbt_graphql/compiler/query.py).

### 9.1 The shape of compilation

Given a GraphQL field like:

```graphql
{
  orders(limit: 10, where: { status: "completed" }) {
    order_id
    amount
    customer {
      customer_id
      name
    }
  }
}
```

the compiler emits a single SQLAlchemy `Select`:

```sql
SELECT
  _parent.order_id AS order_id,
  _parent.amount   AS amount,
  (SELECT JSON_AGG(JSON_OBJECT('customer_id', child.customer_id, 'name', child.name))
     FROM customers AS child
     WHERE child.customer_id = _parent.customer_id)   AS customer
FROM orders AS _parent
WHERE _parent.status = 'completed'
LIMIT 10;
```

### 9.2 Why correlated subqueries, not LATERAL joins

Apache Doris (and some older warehouse engines) do not support `LATERAL`. A correlated subquery is portable everywhere — every SQL dialect supports it, and the optimizer collapses it to the same plan on engines that could use LATERAL anyway. The tradeoff is that ordering/limit on nested fields is harder to express this way; the current compiler doesn't expose those knobs, which is a conscious scope decision.

### 9.3 Dialect-aware JSON aggregation

Different engines have different JSON aggregation functions. Rather than branch in Python, we define marker classes `json_agg` and `json_build_obj` (`FunctionElement` subclasses) and register per-dialect `@compiles` functions:

| Dialect      | `json_agg`          | `json_build_obj`        |
|--------------|---------------------|-------------------------|
| PostgreSQL   | `JSONB_AGG`         | `JSONB_BUILD_OBJECT`    |
| MySQL/MariaDB| `JSON_ARRAYAGG`     | `JSON_OBJECT`           |
| SQLite       | `JSON_GROUP_ARRAY`  | `JSON_OBJECT`           |
| DuckDB       | `LIST`              | `JSON_OBJECT`           |
| default      | `JSON_ARRAYAGG`     | `JSON_OBJECT`           |

SQL generation stays dialect-agnostic until the moment of rendering. This is idiomatic SQLAlchemy — no strings until the very end.

### 9.4 `compile_query()` walkthrough

Inputs: a `TableDef`, the GraphQL field node list, the `TableRegistry`, plus optional `limit` / `offset` / `where` / `max_depth`.

1. `_extract_scalar_fields()` partitions the selection into direct columns (`scalars`) and FK-backed relations.
2. For each relation: `_build_correlated_subquery` builds a correlated subquery that aggregates child rows into a JSON array, correlated on the FK equality. The subquery labels itself with the relation field name so the result JSON has the right shape.
3. **Multi-hop nesting** — `_build_correlated_subquery` is recursive. Each level gets a unique alias (`child_1`, `child_2`, …). Two safety rails apply: a `visited` frozenset prevents any model from appearing twice in the same subquery stack (cycle guard), and an optional `max_depth` parameter (default: unlimited) caps nesting depth for performance. This mirrors the approach Hasura uses for Postgres: a single query with recursively nested correlated subqueries and JSON aggregation at each level — no N+1, no application-side reassembly.
4. `where` is a flat dict of `{col_name: value}` applied as equality predicates. No operator support today — next on the roadmap.
5. `LIMIT` and `OFFSET` applied straight through to SQLAlchemy.

What is **not** supported (explicitly):

- Filtering / ordering on nested fields.
- Operators beyond `=` in `where`.
- Aggregates, group-by, metrics — this is the job of a semantic layer (Cube, MetricFlow), not this compiler.

### 9.5 Connection management (`compiler/connection.py`)

`DatabaseManager` owns an async SQLAlchemy 2.0 engine, exposes `execute()` for a `Select` and `execute_text()` for raw SQL, and tracks the dialect name for downstream code that needs to branch on it. `build_db_url()` accepts either a SQLAlchemy URL directly or a YAML config dict, mapping config `type:` keys to async drivers (`aiomysql`, `asyncpg`, `aiosqlite`). SQLite is special-cased (file path in host, no auth).

No dbt profiles parser — the database configuration is deliberately decoupled from dbt. A dbt project's `profiles.yml` describes how dbt connects during transformation; a production serve layer connects differently (different credentials, pooling, network) and we don't force the two to collide.

---

## 10. Serve layer (`serve/`)

[`src/dbt_graphql/serve/app.py`](../src/dbt_graphql/serve/app.py)

FastAPI + Ariadne, served via `granian` (Rust-based ASGI server).

### 10.1 Assembling the Ariadne schema

`db.graphql` uses standard GraphQL scalars (`Int`, `Float`, `Boolean`, `String`) and custom directives. Ariadne needs a clean executable schema without those directives. `_build_ariadne_sdl()`:

1. Parses `db.graphql`.
2. Collects any type names that aren't standard GraphQL scalars and declares them as `scalar` (rare — most types map to `Int`, `Float`, `Boolean`, or `String`).
3. Builds a per-table `XxxWhereInput` input type for filtering.
4. Builds a `Query` type with one field per table, each accepting `limit: Int`, `offset: Int`, and `where: XxxWhereInput`.

This is a deliberate separation: `db.graphql` is the *description* of the warehouse; the Ariadne schema is the *executable GraphQL schema* derived from it at runtime.

### 10.2 Lifecycle

`create_app()` builds the FastAPI app, mounts `/graphql`, and uses an `@asynccontextmanager` lifespan to connect/close the `DatabaseManager`. State that resolvers need (`TableRegistry`, `DatabaseManager`) is attached to `info.context` — never closure-captured, never module-global.

### 10.3 Resolvers

`serve/resolvers.py` registers one resolver per table. Each resolver:

1. Pulls the `TableDef` out of the registry.
2. Calls `compile_query()` with the GraphQL field nodes, `limit`, `offset`, `where` from kwargs.
3. Executes via the `DatabaseManager`, returns rows as dicts.

That's all. No N+1 issues because nested relations are resolved inside the same query via the correlated-subquery mechanism.

---

## 11. MCP server (`mcp/`)

[`src/dbt_graphql/mcp/server.py`](../src/dbt_graphql/mcp/server.py), [`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

This is the surface LLM agents actually use. It's structured around **how agents plan queries**, not around the HTTP API.

### 11.1 Tools

| Tool                                | Purpose                                                                 |
|-------------------------------------|-------------------------------------------------------------------------|
| `list_tables`                       | All tables with name, description, column count, relationship count.    |
| `describe_table(name)`              | Full column details, constraints, directly related tables.              |
| `find_path(from_table, to_table)`   | Shortest join path(s) via BFS on the relationship graph.                |
| `explore_relationships(table_name)` | All directly related tables with direction (outgoing / incoming).       |
| `build_query(table, fields)`        | Generate a boilerplate GraphQL query for a table and field list.        |
| `execute_query(sql)`                | Run SQL against the warehouse (requires `--db-url`).                    |

Each response includes `_meta.next_steps` — a short list guiding the agent's next tool call. This encodes the expected workflow (list → describe → find_path → build_query → execute_query) in the tool surface itself, which dramatically reduces the need for system-prompt engineering on the agent side.

### 11.2 `SchemaDiscovery` — the engine behind the tools

[`src/dbt_graphql/mcp/discovery.py`](../src/dbt_graphql/mcp/discovery.py)

- Builds a **bidirectional adjacency list** at construction time: every `RelationshipInfo` becomes two edges (outgoing from `from_model`, incoming to `to_model`). BFS traversal works in either direction.
- `find_path()` runs BFS, early-terminating when a longer path would extend. Returns *all* shortest paths, not just one — an agent benefits from seeing alternatives (`orders → customers` vs. `orders → payments → customers`).
- Live-DB enrichment (`get_row_count`, `get_distinct_values`, `get_date_range`, `get_sample_rows`) is implemented but not yet wired into tool outputs — future work.

### 11.3 Why MCP-first matters

An HTTP GraphQL endpoint assumes the consumer knows what to ask. An MCP surface assumes the consumer is *learning what to ask*. The latter is the agent workflow. GraphJin added MCP in v3 and positions it as the primary agent interface; Wren Engine similarly ships an MCP server on top of its MDL; we adopt the same pattern here.

---

## 12. CLI (`cli.py`)

[`src/dbt_graphql/cli.py`](../src/dbt_graphql/cli.py)

Three subcommands, minimal surface area:

- **`generate`** — `extract_project()` + `format_graphql()` + `ProjectInfo.build_lineage_schema()`. Writes `db.graphql` and `lineage.json`.
- **`serve`** — loads `db.graphql`, builds the FastAPI app via `create_app()`, runs under granian.
- **`mcp`** — `extract_project()`, optional `DatabaseManager`, `serve_mcp()` over stdio.

No config file format beyond the existing `db.yml` for `serve`. Everything else is flags. If a flag set grows, it's time to revisit — not today.

---

## 13. Cross-cutting design notes

A short list of decisions you'd only notice if you were reading the code carefully.

1. **Directives encode metadata the SDL readers need.** `@sql`, `@id`, `@unique`, `@relation`, `@database`, `@schema`, `@table` — together they make `db.graphql` self-sufficient. No need to also ship `manifest.json` to production.
2. **Correlated subqueries over LATERAL.** See §9.2.
3. **PascalCase + `@sql`.** Human-readable GraphQL, exact warehouse types. See §3.4.
4. **Next-steps hint pattern.** See §11.1.
5. **Enum deduplication by sorted value set.** `accepted_values(['a','b'])` on two columns becomes one enum, not two.
6. **Read-only.** See §3.5. If this ever changes, it's a major version.
7. **Bidirectional relationship adjacency for BFS.** See §11.2.
8. **Database config decoupled from dbt profiles.** See §9.5.
9. **Relationship origin tiers.** Every `RelationshipInfo` records where it came from (`constraint` > `data_test` > `lineage`, as a `RelationshipOrigin` StrEnum). Constraints and tests are user-authored; lineage-inferred relationships are best-effort and can be filtered by consumers that want only explicitly-declared edges. See §5.2, §5.3, §5.4.
10. **Processor modules are source-based, not output-based.** `constraints.py` / `data_tests.py` / `compiled_sql.py` each correspond to one input surface (dbt constraints / dbt data tests / dbt's compiled SQL). Some modules emit more than one output shape — that's a deliberate choice: when you're chasing a bug, "where did this fact come from?" is the question that matters.
11. **Column lineage via dbt-colibri's traversal approach.** Column lineage uses the recursive `to_node()` traversal originally developed in dbt-colibri (itself a fork of sqlglot's own lineage module, MIT licensed). The core logic — qualify → build_scope → recursive CTE/subquery/UNION/PIVOT resolution with max-rank classification — is absorbed into `compiled_sql.py` to keep the dependency footprint small while retaining correct handling of complex SQL patterns.

---

## 14. Prior art & inspirations

dbt-graphql sits at the intersection of two conceptual lineages. Both are explicit influences.

### 14.1 GraphJin ([dosco/graphjin](https://github.com/dosco/graphjin))

GraphJin is a Go-based compiler that turns GraphQL queries into a single optimized SQL statement. From GraphJin we take:

- The **single-query compilation model** — don't resolve relations with N+1 queries; emit JSON-aggregated correlated subqueries (or the equivalent) so nested GraphQL selections become one SQL statement.
- The **FK-driven relationship inference** — foreign keys (in our case, dbt `relationships` tests + constraints) define navigable edges in GraphQL.
- The **MCP-as-agent-surface** model — GraphJin v3 added an `mcp` subcommand exposing tools like `list_tables`, `describe_table`, `execute_graphql`; dbt-graphql adopts the same shape.

Where we differ: GraphJin introspects the **live database**; dbt-graphql introspects **dbt artifacts**. GraphJin has mutations; dbt-graphql doesn't. GraphJin has role-based authorization in YAML; dbt-graphql has none yet (by design — policy is deferred to whatever sits in front of the serve layer).

### 14.2 Wren Engine / WrenAI ([Canner/wren-engine](https://github.com/Canner/wren-engine), [Canner/WrenAI](https://github.com/Canner/WrenAI))

Wren Engine is a Rust+DataFusion-based semantic context layer with an MDL (Model Definition Language) and a strong AI-agent story. From Wren we take:

- The thesis that **LLMs benefit from a typed, declarative model graph** rather than raw `information_schema`.
- The positioning of an **MCP server as the native agent interface** to that model graph.
- The **native dbt integration** pattern — Wren added a dbt adapter in October 2025 that reads `manifest.json` and `catalog.json` and projects them into MDL; dbt-graphql does the analogous projection into GraphQL SDL.

Where we differ: Wren's interface to agents is "write SQL against MDL" (text-to-SQL); dbt-graphql's is "write GraphQL and we compile it" (no LLM SQL emission). Wren is a federated engine across 15+ data sources; dbt-graphql targets the single warehouse your dbt project already models. Wren ships a full GenBI UI (WrenAI); dbt-graphql is a library + CLI.

### 14.3 Other influences

- **[`dbterd`](https://github.com/datnguye/dbterd)** — the pattern of consuming `manifest.json` + `catalog.json` to reverse-engineer ERDs. Relationship test → FK inference comes from this tradition.
- **[`dbt-colibri`](https://github.com/Datatonic/dbt-colibri)** — a reference implementation of sqlglot-based column lineage over dbt artifacts. Its `lineage.py` is itself a modified fork of sqlglot's own lineage module (MIT). We absorbed the core recursive `to_node()` traversal (per-model subset schema, qualify-then-scope, CTE/subquery/UNION/PIVOT resolution, max-rank classification) directly into `compiled_sql.py` rather than taking a runtime dependency, so the semantics stay under our control and the stack stays minimal.
- **[PostGraphile](https://postgraphile.org)** — the principle that a schema-driven GraphQL API is a *projection* of a richer upstream model, not a hand-authored artifact. PostGraphile projects from Postgres catalogs; we project from dbt artifacts.

---

For a side-by-side comparison against Cube, MetricFlow, Malloy, Vanna, LangChain/LlamaIndex, Hasura, PostGraphile, pg_graphql, Dgraph, AppSync, and an honest strengths/gaps list — see [comparison.md](comparison.md).
