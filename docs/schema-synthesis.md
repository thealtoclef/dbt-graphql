# Schema Synthesis

The phase that reads dbt artifacts and produces a `TableRegistry` — the Python object the serve layer consumes directly. In `--output` mode it also serialises `db.graphql` and `lineage.json` as side artifacts.

**Entry point:** [`src/dbt_graphql/pipeline.py`](../src/dbt_graphql/pipeline.py) — `extract_project()`.

See [architecture.md](architecture.md) for the pipeline overview and design principles that govern this phase.

---

## Table of contents

- [1. Extraction steps](#1-extraction-steps)
- [2. Intermediate representation (`ir/models.py`)](#2-intermediate-representation-irmodelspy)
- [3. dbt processors (`dbt/`)](#3-dbt-processors-dbt)
- [4. Lineage extraction](#4-lineage-extraction)
- [5. SDL emission (`formatter/graphql.py`)](#5-sdl-emission-formattergraphqlpy)
- [6. SDL parsing & `TableRegistry` (`formatter/schema.py`)](#6-sdl-parsing--tableregistry-formatterschemapy)

---

## 1. Extraction steps

`extract_project()` runs ten steps in sequence:

1. **Load artifacts.** `load_catalog()` / `load_manifest()` wrap `dbt_artifacts_parser.parser`. Returns typed nodes; no schema validation beyond what the parser provides.
2. **Read project metadata.** `project_name`, `adapter_type` from `manifest.metadata` — used to label the lineage output and to feed dialect-aware SQL generation.
3. **Preprocess tests.** Scans `manifest.nodes` for `not_null`, `unique`, `accepted_values` tests. Produces a `TestsResult` keyed by `"{unique_id}.{column_name}"`. Enum definitions are deduplicated by sorted value tuple so two columns with the same `accepted_values` list reuse one enum (name collisions get a numeric suffix).
4. **Extract constraints.** Reads dbt v1.5+ `constraints` on models (table-level PK / FK) and on columns (column-level). Supports both the legacy `expression="other_table(col)"` form and the v1.9+ `to="db.schema.other_table"` + `to_columns=[...]` form.
5. **Build models.** For every `model.*` node in the catalog: strip SQL quoting off column names; attach raw SQL types, `not_null`, `unique`, and `enum_values` from the tests result; pull descriptions from the manifest; sort columns by catalog index then name; fall back `database → schema` for adapters that don't populate database (MySQL).
6. **Merge relationships.** Three sources in priority order: dbt v1.5+ `constraints` > `relationships` tests > JOIN-ON mining (sqlglot). Each `ProcessorRelationship` carries an `origin` tag (`constraint` | `data_test` | `lineage`) that propagates to the IR. Deduplication is by relationship name: whichever tier lands first wins.
7. **Attach relationships to models.** Each `RelationshipInfo` ends up on both the from-model and the to-model so downstream consumers can navigate edges without rebuilding an index.
8. **Extract lineage.** Table lineage from `depends_on.nodes` for every model. Column lineage built with sqlglot directly — no optional-dependency gate.
9. **Build enums dict.** Flattens `{enum_name: [values]}` for formatters that want it.
10. **Return `ProjectInfo`.** The single boundary between extraction and everything that follows.

Once `ProjectInfo` is in hand, the rest of the system is deterministic: `build_registry()` → `TableRegistry`, compile GraphQL selections → SQL, execute. In `--output` mode, `_registry_to_sdl()` serialises `TableRegistry` to SDL and writes `db.graphql`.

---

## 2. Intermediate representation (`ir/models.py`)

[`src/dbt_graphql/ir/models.py`](../src/dbt_graphql/ir/models.py)

Everything between extraction and the final outputs speaks IR.

### Core types

Four Pydantic models carry the full dbt semantics into the rest of the system:

- **`ColumnInfo`** — name, SQL type, not_null, unique, enum_values, description.
- **`ModelInfo`** — name, database/schema, columns, primary_keys, relationships, description.
- **`RelationshipInfo`** — from/to model + column, join_type, origin. Carried on both endpoints.
- **`ProjectInfo`** — all models, enums, table lineage, column lineage; exposes `build_lineage_schema()`.

Two deliberate choices:

- **`primary_keys` lives on the model, not on columns.** A primary key is a table-level constraint. Storing PK membership per-column would duplicate information and invite drift.
- **`type` is required, no default.** An empty `type` produces a broken SDL. Required-at-construction surfaces the problem at the source.

### Lineage types

`LineageType` is a `StrEnum` with values `pass_through`, `rename`, `transformation`, `filter`, `join`, `unknown`. `ProjectInfo.build_lineage_schema()` groups raw column-lineage dicts by `(source, target)`, constructs typed `ColumnLineageItem` entries, and validates via Pydantic. JSON serialization uses `by_alias=True` so the output is camelCase (`sourceColumn`, `lineageType`) — friendlier to downstream JavaScript tooling.

---

## 3. dbt processors (`dbt/`)

Three source-based processors, each responsible for one input surface. The module name tells you where the data came from, not what shape it has.

### `dbt/artifacts.py`

Thin wrapper around `dbt_artifacts_parser.parser`. Returns typed manifest/catalog objects. The only rule: if someone wants to swap the upstream source (e.g., a dbt Cloud API), this is the only file they needs to change.

### `dbt/processors/constraints.py` — dbt v1.5+ constraints

Extracts `constraints` on models and columns.

- **Primary keys**: both `constraints[].type == "primary_key"` on the model *and* column-level constraints. Results merged, deduplicated by unique_id.
- **Foreign keys, legacy form** (pre-v1.9): `expression="other_table(other_col)"`, `columns=["my_col"]`. Parsed with a regex in `_parse_fk_expression()`.
- **Foreign keys, modern form** (dbt v1.9+): `to="db.schema.other_table"`, `to_columns=["other_col"]`, `columns=["my_col"]`. Resolved to a model name via `_resolve_to_model()` which searches manifest nodes by `relation_name`.

Output is a list of `ProcessorRelationship` (dataclass) with `join_type` defaulting to `many_to_one` and `origin=RelationshipOrigin.constraint`. Cardinality refinement happens in `pipeline._rel_to_domain()`.

### `dbt/processors/data_tests.py` — dbt data tests

Extracts everything authored as a dbt **data test** (schema test). Not to be confused with dbt *unit tests*.

- `not_null` / `unique` → booleans keyed by `"{unique_id}.{column_name}"`.
- `accepted_values` → enum definitions. Enum name derived from the column name via `_sanitize_enum_name()`. Deduplication is by **sorted value tuple** — two columns with the same `accepted_values` list share one enum.
- `relationships` → `ProcessorRelationship` objects (origin `"data_test"`). For each test node: `attached_node` → source unique_id, `column_name` → source column, `refs[0].name` → target model, `test_metadata.kwargs["field"]` → target column.

Adding support for a new data test type is localized to this module.

### `dbt/processors/compiled_sql.py` — sqlglot over compiled SQL

Centralized sqlglot-driven extraction. Three public functions sharing a single parsing pipeline:

- `extract_table_lineage(manifest)` — table edges from `depends_on.nodes`. No SQL parsing, but lives here because "what came from the compiled DAG" is the conceptual fit.
- `extract_column_lineage(manifest, catalog)` — column-level lineage. See [§4](#4-lineage-extraction).
- `extract_join_relationships(manifest, catalog)` — FK-style relationships mined from JOIN ON clauses. For each model's compiled SQL, walks every nested scope and extracts equality pairs from each JOIN ON. Each column reference is resolved through CTE/subquery scopes to a leaf table, which is mapped to a dbt model name via the shared `table_lookup`. Direction rule: the dbt model being processed is always the `from_model`. Emitted with `origin=RelationshipOrigin.lineage` — lowest priority, so an explicit constraint or `relationships` test on the same pair always wins.

### Relationship cardinality inference

Done in `pipeline._infer_join_type()`:

| `from` column unique | `to` column unique | join_type        |
|----------------------|--------------------|------------------|
| yes                  | yes                | `one_to_one`     |
| yes                  | no                 | `one_to_many`    |
| no                   | yes                | `many_to_one`    |
| no                   | no                 | `many_to_one` *  |

\* Fallback: when uniqueness is unknown on both sides, we assume the standard FK pattern. A column is "unique" if it has a `unique` test or is a single-column PK.

---

## 4. Lineage extraction

[`src/dbt_graphql/dbt/processors/compiled_sql.py`](../src/dbt_graphql/dbt/processors/compiled_sql.py)

### Table-level

For every `model.*` node, emit an edge for each entry in `depends_on.nodes` that starts with `model.`, `seed.`, or `source.`. Trivial and deterministic — no heuristics, no SQL parsing.

### Column-level (via sqlglot)

For every materialized model:

1. Build a per-model schema dict `{database: {schema: {table: {col: type}}}}` restricted to `depends_on.nodes`.
2. Sanitize the compiled SQL (dialect-specific: Oracle `LISTAGG DISTINCT` / `ON OVERFLOW` stripping).
3. Parse with the detected sqlglot dialect (`"sqlserver"` → `"tsql"`; Postgres strips quoted identifiers so `SELECT *` can expand).
4. `qualify()` with `validate_qualify_columns=False` and `identify=False` — the goal is scope construction, not a validated rewrite.
5. `build_scope()` → recursively trace each outer select through CTE/subquery scopes to leaf `exp.Table` nodes, classifying each hop (`pass_through` / `rename` / `transformation`) and taking the max rank across the chain.

The core recursive `to_node()` traversal (qualify → build_scope → recursive CTE/subquery/UNION/PIVOT resolution, max-rank classification) is derived from dbt-colibri, itself a fork of sqlglot's lineage module (MIT). It's absorbed directly into `compiled_sql.py` to keep the dependency footprint small.

### Why lineage is first-class

No DB-introspection tool (Hasura, pg_graphql, PostGraphile) can produce lineage — it doesn't exist in `pg_catalog`. It only exists in the transformation graph, which is exactly what dbt owns. Exposing it alongside the schema means an agent can ask not just "what columns are on this model" but "where does this column come from."

---

## 5. Schema object + SDL emission (`formatter/graphql.py`)

[`src/dbt_graphql/formatter/graphql.py`](../src/dbt_graphql/formatter/graphql.py)

Three functions with a clear split of responsibility:

- **`build_registry(project)`** — the canonical path. Converts `ProjectInfo` directly to a `TableRegistry` (Python dataclasses). This is what the serve layer uses; no file I/O, no SDL roundtrip.
- **`_registry_to_sdl(registry)`** — serialises a `TableRegistry` to SDL string. Used only in `--output` mode to write `db.graphql`.
- **`format_graphql(project)`** — convenience composition of the two above, used in tests and the `--output` export path.

One `ModelInfo` becomes one `type` block in SDL.

### Type-level directive

```graphql
type orders @table(database: "mydb", schema: "public", name: "orders") {
  ...
}
```

`@table(database:, schema:, name:)` maps the GraphQL type to the physical warehouse table, honoring the dbt `alias` if set. This makes `db.graphql` self-sufficient — no need to ship `manifest.json` to production.

### Field-level directives

For each column:

- `@column(type: "...", size: "...")` — **always present.** Preserves the raw SQL type and any size/precision.
- `@unique` — column has a `unique` test and is not already the sole PK.
- `@relation(type: TargetModel, field: target_col)` — foreign key.

### Type mapping

`_parse_sql_type()` turns raw SQL types into `(base, size, is_array)`:

| Raw SQL                    | base                       | size    | is_array |
|----------------------------|----------------------------|---------|----------|
| `INTEGER`                  | `INTEGER`                  | ``      | false    |
| `VARCHAR(255)`             | `VARCHAR`                  | `255`   | false    |
| `NUMERIC(10,2)`            | `NUMERIC`                  | `10,2`  | false    |
| `DOUBLE PRECISION`         | `DOUBLE PRECISION`         | ``      | false    |
| `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP WITH TIME ZONE` | ``      | false    |
| `TEXT[]` (Postgres)        | `TEXT`                     | ``      | true     |
| `ARRAY<STRING>` (BigQuery) | `STRING`                   | ``      | true     |

`_sql_to_gql_scalar()` maps the base to a standard scalar: boolean → `Boolean`, anything ending in `INT` / `INTEGER` / `INT64` → `Int`, numeric/float → `Float`, everything else → `String`.

### What is *not* emitted

- No scalar definitions — all field types are standard GraphQL scalars.
- No query root — `db.graphql` describes the warehouse; the query root is added at serve time.

---

## 6. SDL parsing & `TableRegistry` (`formatter/schema.py`)

[`src/dbt_graphql/formatter/schema.py`](../src/dbt_graphql/formatter/schema.py)

At serve time and compile time, `db.graphql` is re-parsed into typed Python objects (`ColumnDef`, `TableDef`) — the inverse of the formatter. `TableRegistry` is a dict-like wrapper: `registry[name]`, `name in registry`, `iter(registry)`. The compiler and MCP layer both look up tables through it.

Parsing uses `graphql-core`. `_unwrap_type()` walks `NonNullTypeNode` → `ListTypeNode` → `NamedTypeNode` to compute `(type_name, not_null, is_array)`. `_directive_args()` flattens directive arguments into a `dict[str, str]`.

**When is parsing used?** The primary serve path does not use `parse_db_graphql` — it calls `build_registry()` directly from dbt artifacts. `parse_db_graphql` is still useful when you have a pre-built `db.graphql` (e.g. a CI-generated artifact shipped to production without dbt artifacts) and want to reconstruct a `TableRegistry` from it without re-running extraction.
