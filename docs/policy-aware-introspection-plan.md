# Policy-Aware Introspection — Implementation Plan

## Goal

Standard GraphQL introspection at `/graphql` reflects the caller's policy
view: tables/columns they can't access don't appear in `__schema`, and
columns/tables affected by masking or row filtering are tagged so that
clients can render the treatment to the user.

The plan is delivered in two passes. **Pass 1** (this doc, the section
below labelled "This pass") lands the schema-level pieces that don't
depend on per-request execution: type-level signals (PK as `ID`, dbt
descriptions, `@masked` / `@filtered` directives) and the registry
fields that carry the per-principal flags. **Pass 2** ("Deferred —
revisit") wires those flags per request and ships the matching SDL
delivery contract for query-builder UIs.

---

## How signals reach introspection clients

Standard GraphQL `IntrospectionQuery` exposes a fixed, spec-defined set
of fields on `__Type` / `__Field`. **Applied directives are not in
that set**. graphql-core follows the spec; there is no
`appliedDirectives` field. So a custom directive on a field is invisible
to GraphiQL / Apollo Studio / codegen unless we route the signal
through one of the carriers introspection actually exposes.

Carrier inventory:

| Signal                                         | Native introspection? | Carrier                                       |
|------------------------------------------------|-----------------------|-----------------------------------------------|
| Field type (Int / Float / String / Boolean)    | ✅ yes                | the type itself                               |
| Nullability (`not_null`)                       | ✅ yes                | `NON_NULL` wrapper                            |
| Array / list                                   | ✅ yes                | `LIST` wrapper                                |
| Description (dbt model / column descriptions)  | ✅ yes                | type / field `description`                    |
| `@id` (primary key)                            | ✅ yes (if mapped)    | map PK → built-in `ID` scalar                 |
| `@unique`                                      | ❌ no                 | TBD — see "Deferred"                          |
| `@column(type, size)` (SQL type/size)          | ❌ no                 | TBD — see "Deferred"                          |
| `@table(database, schema, name)`               | ❌ no                 | TBD — see "Deferred"                          |
| `@relation` (FK metadata)                      | ❌ no                 | TBD — see "Deferred"                          |
| `@masked`                                      | ❌ no                 | TBD — see "Deferred"                          |
| `@filtered`                                    | ❌ no                 | TBD — see "Deferred"                          |

The custom directives `@table`, `@column`, `@unique`, `@relation`
already do not reach introspection clients today; they only appear in
the printed SDL artefact (`--output` mode). That is unchanged by this
plan. The decision about *how* to carry the non-native signals to
introspection clients — description-prefix vs. a side channel
(`Query._schema_sdl`, separate endpoint, …) — is deferred.

What changes immediately: PKs are emitted as `ID`, so the primary-key
signal becomes native. The `@id` directive is dropped from the SDL.

---

## This pass — landed in this change

### 1. Drop live-DB enrichment from MCP

Live enrichment (row counts, sample rows, value summaries) issues
warehouse queries from the schema-discovery surface. Reasons to remove:

- Unbounded warehouse cost on a metadata surface; gets worse with table
  count.
- Leaks data into a surface that's supposed to describe shape only.
- We want one source of truth: the dbt manifest. A future profile
  artefact (offline-produced JSON consumed alongside the manifest) can
  layer richer summaries back in without live queries.

Removed:

- `mcp/discovery.py`: drop the `db=` parameter, drop the `_Enrichment`
  indirection class, drop all live `count(*)` / sample / value-summary
  code paths. Fold dbt-derived fields (table description, column
  description, declared enums) directly onto the `TableDef` /
  `ColumnDef` at registry-build time so discovery reads from the
  registry only.
- `mcp/server.py`: drop the `db=` plumbing into `SchemaDiscovery`.
  `describe_table` returns only static manifest-derived data.
- `EnrichmentConfig` (in `config.py`) and the `ENRICHMENT_*` defaults.
- `tests/integration/test_mcp_enrichment.py` (entirely) plus the
  `TestEnrichmentBudget` / `TestDistinctValuesKeyMismatch` suites in
  `tests/unit/mcp/test_discovery.py`.

Kept:

- dbt manifest descriptions on tables and columns.
- Relationship metadata derived from dbt (already in `RelationDef`).
- Primary-key / unique markers (already in `ColumnDef`).
- Static enum summaries derived from dbt's declared `enum_values` (no
  DB needed — these come from the manifest).

### 2. PK → `ID`; drop `@id` directive

Before: PK columns are emitted with their native scalar (`Int`, `String`)
plus an `@id` directive.

After: PK columns are emitted with the built-in `ID` scalar. No `@id`
directive. `ID` is wire-compatible with `String` per the spec; the
underlying SQL type is still preserved on `ColumnDef.sql_type` and is
reflected in the description prefix in pass 2 (deferred).

This gives every introspection client native PK semantics with no
custom directive needed.

### 3. dbt descriptions in the SDL

`TableDef` and `ColumnDef` gain `description: str = ""` (default empty)
populated from `ProjectInfo.models[].description` and
`ProjectInfo.models[].columns[].description` in `build_registry`. The
SDL serializer emits them as standard triple-quoted blocks above the
type / field. `parse_db_graphql` reads them back so a round-trip is
clean.

No directive serialization is prefixed onto the description in this
pass — that decision is part of the deferred work.

### 4. `masked` / `filtered` flags + SDL directives

`ColumnDef` gains `masked: bool = False`. `TableDef` gains
`filtered: bool = False`. Default false; nothing populates them yet —
that's deferred to the per-request filter.

The SDL builder honours the flags: when `masked` is true, emit
`@masked` on the field; when `filtered` is true, emit `@filtered` on
the type. The SDL also declares the directives at the head:

```graphql
directive @masked on FIELD_DEFINITION
directive @filtered on OBJECT
```

Effect on the `--output` artefact today: nothing (no caller, no flags
set). The plumbing exists so pass 2 can light it up by calling a
helper that sets the flags per principal.

---

## Deferred — revisit

These items are out of scope for this pass. They will be re-planned
once we've decided on the introspection carrier for non-native signals
and the SDL delivery contract for query-builder UIs. Open questions:

- **Per-principal application of `masked` / `filtered`.** A
  `filter_registry_for(registry, engine, jwt) -> TableRegistry` helper
  that copies the registry, omits denied tables / columns, and sets the
  flag fields. Used by both HTTP and MCP so they share one filter path.
- **Per-request schema build.** Replace the startup-singleton
  `make_executable_schema` with a per-request build from the filtered
  registry. Replace `ariadne.asgi.GraphQL` with a small custom ASGI
  handler. Drop the `introspection: bool` flag — introspection is
  always on, bounded by the per-principal schema.
- **MCP unification.** Replace the per-call `_is_visible` /
  `_column_visible` in `mcp/server.py` with a single
  `filter_registry_for` call at the top of each MCP tool.
- **Introspection carrier for `@unique`, `@column(type,size)`,
  `@table(...)`, `@relation`, `@masked`, `@filtered`.** Candidates:
  - Description prefix: serialize the SDL form of each non-native
    directive at the top of the field/type description, blank line,
    then the dbt description. Visible to every standard introspection
    client; query-builder UIs can parse the prefix back if they want
    structured form.
  - `Query._schema_sdl: String!` resolver: returns the per-principal
    SDL string with directives intact for query-builder UIs.
  - Separate `GET /graphql/sdl` endpoint.
  - Map relations to nested object fields so the FK becomes native.
- **Performance metric.** OTel histogram
  `graphql.schema_build.duration` once per-request build is in place.

The deferred items are tracked together because each one's design
depends on the introspection-carrier decision.

---

## Tests for this pass

All under `tests/`. Real fixture data, no mocks for new behaviour.
Run with `uv run pytest --cov=dbt_graphql --cov-report=term-missing`.

- `tests/unit/formatter/test_formatter.py` — extend:
  - PK columns emit `ID` and no `@id` directive.
  - dbt descriptions emitted as triple-quoted blocks above types and
    fields.
  - `ColumnDef.masked = True` → `@masked` appears on that field.
  - `TableDef.filtered = True` → `@filtered` appears on that type.
  - Directive declarations for `@masked` / `@filtered` present at the
    head of the SDL.
- `tests/unit/formatter/test_schema.py` — extend:
  - Round-trip: SDL with descriptions / `@masked` / `@filtered` parses
    back to a registry that preserves the flags and descriptions.
  - PK column without `@id` (now emitted as `ID`) is still recognised
    as a PK on parse.
- `tests/unit/mcp/test_discovery.py` — trim:
  - Drop `TestEnrichmentBudget` and `TestDistinctValuesKeyMismatch`.
  - Adjust `test_no_db_returns_null_enrichment` to verify the now-fixed
    "no DB" shape (no `row_count`, no `sample_rows`).
- `tests/integration/test_mcp_enrichment.py` — deleted.

---

## Documentation

- `docs/graphql.md`: note that PKs are emitted as `ID`; dbt descriptions
  flow into the SDL; the `@masked` / `@filtered` directives exist and
  what they signal (flag-only, no expression / predicate exposed).
- `docs/mcp.md`: drop live-enrichment language; describe the new
  manifest-only contract.
- `ROADMAP.md`: track the deferred items as one umbrella entry.
- `README.md`: untouched — scope is too small.
