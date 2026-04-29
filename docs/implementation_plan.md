# Implementation Plan: SOTA Query Layer

## Overview

Implement a best-in-class GraphQL query layer on top of dbt-graphql. The design draws from Cube, GraphJin, and PostGraphile — prioritising minimal SDL surface area, single-entry-point per table, and inline aggregate access without separate root fields.

Core principles:
- One root entry point per table (`{T}` → `{T}Result`)
- Aggregates inline on the result envelope — no `{T}_aggregate` root pollution
- GROUP BY is a field on the same envelope (`group`), not a separate root field
- Flat aggregate naming (`sum_Total`, `avg_Total`) — no nested sub-types
- `{T}_bool_exp` recursive filter tree (Hasura-style operators, GraphJin `_or`/`_and`/`_not`)
- All features cross-dialect (PostgreSQL + MySQL/Doris); dialect gates forbidden

Two complementary query capabilities ship in one commit:
- **Enhanced WHERE + ORDER BY** on `nodes`
- **Inline aggregates + GROUP BY** on the result envelope

---

## Target SDL (Complete Reference)

Using `Invoice` (5 columns: `InvoiceId: ID`, `CustomerId: Int`, `InvoiceDate: String`, `BillingState: String`, `Total: Float`) as example.

```graphql
# ── Shared scalars — emitted once per schema ──────────────────────────────

input String_comparison_exp {
  _eq: String    _neq: String
  _gt: String    _gte: String
  _lt: String    _lte: String
  _in: [String!] _nin: [String!]
  _is_null: Boolean
  _like: String  _nlike: String
  _ilike: String _nilike: String
}

input Int_comparison_exp {
  _eq: Int    _neq: Int
  _gt: Int    _gte: Int
  _lt: Int    _lte: Int
  _in: [Int!] _nin: [Int!]
  _is_null: Boolean
}

input Float_comparison_exp {
  _eq: Float    _neq: Float
  _gt: Float    _gte: Float
  _lt: Float    _lte: Float
  _in: [Float!] _nin: [Float!]
  _is_null: Boolean
}

input Boolean_comparison_exp {
  _eq: Boolean
  _is_null: Boolean
}

enum order_by {
  asc
  desc
}

# ── Per-table types (3 types per table, not 14+) ───────────────────────────

input Invoice_bool_exp {
  _and: [Invoice_bool_exp!]
  _or:  [Invoice_bool_exp!]
  _not: Invoice_bool_exp
  InvoiceId:    Int_comparison_exp
  CustomerId:   Int_comparison_exp
  InvoiceDate:  String_comparison_exp
  BillingState: String_comparison_exp
  Total:        Float_comparison_exp
}

input Invoice_order_by {
  InvoiceId:    order_by
  CustomerId:   order_by
  InvoiceDate:  order_by
  BillingState: order_by
  Total:        order_by
}

# Flat ORDER BY for group queries — dimensions + aggregate fields, all at top level
input Invoice_group_order_by {
  InvoiceId:    order_by
  CustomerId:   order_by
  InvoiceDate:  order_by
  BillingState: order_by
  Total:        order_by
  count:        order_by
  sum_Total:    order_by
  avg_Total:    order_by
  stddev_Total: order_by
  var_Total:    order_by
  min_Total:    order_by
  max_Total:    order_by
  min_InvoiceDate:  order_by
  max_InvoiceDate:  order_by
  min_BillingState: order_by
  max_BillingState: order_by
}

# ── Result envelope ────────────────────────────────────────────────────────

type InvoiceResult {
  # Row data — own pagination/ordering args
  nodes(order_by: [Invoice_order_by!], limit: Int, offset: Int): [Invoice!]!

  # Scalar aggregates over the filtered set
  count: Int!
  sum_Total:    Float
  avg_Total:    Float
  stddev_Total: Float
  var_Total:    Float
  min_Total:    Float
  max_Total:    Float
  min_InvoiceDate:  String
  max_InvoiceDate:  String
  min_BillingState: String
  max_BillingState: String

  # GROUP BY — Cube-style: GROUP BY auto-derived from selected dimension fields
  group(order_by: [Invoice_group_order_by!], limit: Int, offset: Int): [Invoice_group!]!
}

# Flat group row: all dimension fields (nullable) + same aggregate fields
type Invoice_group {
  InvoiceId:    Int
  CustomerId:   Int
  InvoiceDate:  String
  BillingState: String
  Total:        Float
  count:        Int!
  sum_Total:    Float
  avg_Total:    Float
  stddev_Total: Float
  var_Total:    Float
  min_Total:    Float
  max_Total:    Float
  min_InvoiceDate:  String
  max_InvoiceDate:  String
  min_BillingState: String
  max_BillingState: String
}

# ── Query root ─────────────────────────────────────────────────────────────

type Query {
  Invoice(where: Invoice_bool_exp): InvoiceResult!
}
```

### Type count comparison

| Approach | Types per table |
|----------|----------------|
| Old Hasura-style | 14+ (`Invoice_aggregate`, `Invoice_aggregate_fields`, `Invoice_aggregate_sum`, `Invoice_aggregate_avg`, `Invoice_aggregate_stddev`, `Invoice_aggregate_stddev_pop`, `Invoice_aggregate_stddev_samp`, `Invoice_aggregate_variance`, `Invoice_aggregate_var_pop`, `Invoice_aggregate_var_samp`, `Invoice_aggregate_min`, `Invoice_aggregate_max`, plus order_by sub-types) |
| This plan | 3 (`InvoiceResult`, `Invoice_group`, inputs `Invoice_bool_exp`, `Invoice_order_by`, `Invoice_group_order_by`) |

---

## Query Examples

### Filtered rows with ordering

```graphql
query {
  Invoice(where: { BillingState: { _eq: "CA" }, Total: { _gte: 10.0 } }) {
    nodes(order_by: [{ Total: desc }], limit: 20, offset: 0) {
      InvoiceId
      BillingState
      Total
    }
  }
}
```

### Scalar aggregates over a filtered set

```graphql
query {
  Invoice(where: { BillingState: { _eq: "CA" } }) {
    count
    sum_Total
    avg_Total
  }
}
```

### GROUP BY with auto-derived grouping keys (Cube pattern)

```graphql
# GROUP BY is auto-derived from whichever dimension fields you select.
# Selecting BillingState → GROUP BY BillingState.
query {
  Invoice(where: { Total: { _gte: 5.0 } }) {
    group(order_by: [{ sum_Total: desc }], limit: 10) {
      BillingState   # dimension — drives GROUP BY
      count
      sum_Total
      avg_Total
    }
  }
}
```

### Combining nodes + aggregates in one request

```graphql
query {
  Invoice(where: { BillingState: { _in: ["CA", "NY"] } }) {
    count
    sum_Total
    nodes(order_by: [{ InvoiceDate: desc }], limit: 5) {
      InvoiceId
      Total
    }
  }
}
```

---

## Feature Specifications

### 1. `{T}_bool_exp` — Recursive WHERE

**Current state**: equality-only `{T}WhereInput`. **Target**: recursive bool_exp tree.

**Scalar → comparison_exp mapping** in `app.py`:

```python
_COMPARISON_EXP_TYPES = """\
input String_comparison_exp {
  _eq: String  _neq: String
  _gt: String  _gte: String
  _lt: String  _lte: String
  _in: [String!]  _nin: [String!]
  _is_null: Boolean
  _like: String  _nlike: String
  _ilike: String  _nilike: String
}

input Int_comparison_exp {
  _eq: Int  _neq: Int
  _gt: Int  _gte: Int
  _lt: Int  _lte: Int
  _in: [Int!]  _nin: [Int!]
  _is_null: Boolean
}

input Float_comparison_exp {
  _eq: Float  _neq: Float
  _gt: Float  _gte: Float
  _lt: Float  _lte: Float
  _in: [Float!]  _nin: [Float!]
  _is_null: Boolean
}

input Boolean_comparison_exp {
  _eq: Boolean
  _is_null: Boolean
}\
"""

_GQL_SCALAR_TO_CMP_EXP: dict[str, str] = {
    "String":  "String_comparison_exp",
    "Int":     "Int_comparison_exp",
    "Float":   "Float_comparison_exp",
    "Boolean": "Boolean_comparison_exp",
    "ID":      "Int_comparison_exp",
}
```

**SDL generation** in `_build_ariadne_sdl()` — replaces the old `{T}WhereInput` block:

```python
# bool_exp
lines = [
    f"input {name}_bool_exp {{",
    f"  _and: [{name}_bool_exp!]",
    f"  _or:  [{name}_bool_exp!]",
    f"  _not: {name}_bool_exp",
]
for col in table_def.columns:
    if col.is_array:
        continue
    cmp_exp = _GQL_SCALAR_TO_CMP_EXP.get(col.gql_type, "String_comparison_exp")
    lines.append(f"  {col.name}: {cmp_exp}")
lines.append("}")
bool_exp_defs.append("\n".join(lines))
```

**Breaking change**: `where: {BillingState: "CA"}` → `where: {BillingState: {_eq: "CA"}}`. Pre-1.0, undocumented, acceptable.

**Compiler** — add to `compiler/query.py`:

```python
from sqlalchemy import and_, or_, not_, null

_COMPARISON_OPS: dict[str, Any] = {
    "_eq":      lambda col, v: col == v,
    "_neq":     lambda col, v: col != v,
    "_gt":      lambda col, v: col > v,
    "_gte":     lambda col, v: col >= v,
    "_lt":      lambda col, v: col < v,
    "_lte":     lambda col, v: col <= v,
    "_in":      lambda col, v: col.in_(v),
    "_nin":     lambda col, v: col.not_in(v),
    "_is_null": lambda col, v: col.is_(null()) if v else col.is_not(null()),
    "_like":    lambda col, v: col.like(v),
    "_nlike":   lambda col, v: col.not_like(v),
    "_ilike":   lambda col, v: col.ilike(v),
    "_nilike":  lambda col, v: col.not_ilike(v),
}


def _where_to_clause(
    where: dict,
    aliased,
    tdef: TableDef,
    resolved_policy: ResolvedPolicy | None,
) -> Any:
    clauses = []
    for key, value in where.items():
        if key == "_and":
            clauses.append(
                and_(*[_where_to_clause(b, aliased, tdef, resolved_policy) for b in value])
            )
        elif key == "_or":
            clauses.append(
                or_(*[_where_to_clause(b, aliased, tdef, resolved_policy) for b in value])
            )
        elif key == "_not":
            clauses.append(
                not_(_where_to_clause(value, aliased, tdef, resolved_policy))
            )
        else:
            if resolved_policy is not None:
                _check_column_access(key, tdef, resolved_policy)
            col = aliased.c[key]
            for op, operand in value.items():
                fn = _COMPARISON_OPS[op]
                clauses.append(fn(col, operand))
    return and_(*clauses) if clauses else true()


def _collect_where_columns(where: dict) -> set[str]:
    """Return all column names referenced in a bool_exp tree."""
    cols: set[str] = set()
    for key, value in where.items():
        if key in ("_and", "_or"):
            for branch in value:
                cols |= _collect_where_columns(branch)
        elif key == "_not":
            cols |= _collect_where_columns(value)
        else:
            cols.add(key)
    return cols
```

### 2. `order_by` — Ordered Rows

**SDL generation** in `_build_ariadne_sdl()`:

```python
# order_by input
lines = [f"input {name}_order_by {{"]
for col in table_def.columns:
    if not col.is_array:
        lines.append(f"  {col.name}: order_by")
lines.append("}")
order_by_defs.append("\n".join(lines))
```

**Compiler** — add to `compiler/query.py`:

```python
from sqlalchemy import asc, desc, nulls_first, nulls_last

_ORDER_BY_MAP = {
    "asc":             lambda c: asc(c),
    "asc_nulls_first": lambda c: asc(nulls_first(c)),
    "asc_nulls_last":  lambda c: asc(nulls_last(c)),
    "desc":            lambda c: desc(c),
    "desc_nulls_first":lambda c: desc(nulls_first(c)),
    "desc_nulls_last": lambda c: desc(nulls_last(c)),
}


def _compile_order_by(
    order_by: list[dict],
    aliased,
    tdef: TableDef,
    resolved_policy: ResolvedPolicy | None,
) -> list:
    clauses = []
    for item in order_by:
        for col_name, direction in item.items():
            if resolved_policy is not None:
                _check_column_access(col_name, tdef, resolved_policy)
            order_fn = _ORDER_BY_MAP.get(direction)
            if order_fn is None:
                raise ValueError(f"Unknown order_by direction: {direction!r}")
            clauses.append(order_fn(aliased.c[col_name]))
    return clauses
```

**Updated `compile_query` signature**:

```python
def compile_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    dialect: str = "",
    limit: int | None = None,
    offset: int | None = None,
    where: dict | None = None,
    order_by: list[dict] | None = None,
    max_depth: int | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
```

Inside `compile_query`, replace the old equality-only WHERE block:

```python
resolved_policy = resolve_policy(tdef.name) if resolve_policy else None

# WHERE
if where:
    stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))

# Policy row filter (additive with caller WHERE)
if resolved_policy and resolved_policy.row_filter_clause:
    stmt = stmt.where(resolved_policy.row_filter_clause(aliased))

# ORDER BY
if order_by:
    for clause in _compile_order_by(order_by, aliased, tdef, resolved_policy):
        stmt = stmt.order_by(clause)

# Pagination
if limit is not None:
    stmt = stmt.limit(limit)
if offset is not None:
    stmt = stmt.offset(offset)
```

### 3. Result Envelope (`{T}Result`)

#### SDL generation in `_build_ariadne_sdl()`

The old per-table `{T}WhereInput` and `[{T}]` query field are replaced by:

```python
# Aggregate field names derived from numeric columns
_NUMERIC_TYPES = {"Int", "Float", "ID"}

def _agg_fields_for_table(table_def: TableDef) -> list[tuple[str, str]]:
    """Return (field_name, gql_type) for all flat aggregate fields."""
    fields = []
    for col in table_def.columns:
        if col.is_array:
            continue
        t = col.gql_type
        if t in _NUMERIC_TYPES:
            gtype = "Float" if t == "Float" else "Int"
            fields.append((f"sum_{col.name}", "Float"))
            fields.append((f"avg_{col.name}", "Float"))
            fields.append((f"stddev_{col.name}", "Float"))
            fields.append((f"var_{col.name}", "Float"))
            fields.append((f"min_{col.name}", gtype))
            fields.append((f"max_{col.name}", gtype))
        else:
            # min/max apply to String too (lexicographic)
            fields.append((f"min_{col.name}", t))
            fields.append((f"max_{col.name}", t))
    return fields
```

`{T}Result` type block:

```python
agg_fields = _agg_fields_for_table(table_def)
result_lines = [f"type {name}Result {{"]
result_lines.append(
    f"  nodes(order_by: [{name}_order_by!], limit: Int, offset: Int): [{name}!]!"
)
result_lines.append("  count: Int!")
for fname, ftype in agg_fields:
    result_lines.append(f"  {fname}: {ftype}")
result_lines.append(
    f"  group(order_by: [{name}_group_order_by!], limit: Int, offset: Int): [{name}_group!]!"
)
result_lines.append("}")
result_type_blocks.append("\n".join(result_lines))
```

`{T}_group` type block:

```python
group_lines = [f"type {name}_group {{"]
for col in table_def.columns:
    if col.is_array:
        continue
    group_lines.append(f"  {col.name}: {col.gql_type}")
group_lines.append("  count: Int!")
for fname, ftype in agg_fields:
    group_lines.append(f"  {fname}: {ftype}")
group_lines.append("}")
group_type_blocks.append("\n".join(group_lines))
```

`{T}_group_order_by` input block (flat — dimensions + aggregates at same level):

```python
grp_ob_lines = [f"input {name}_group_order_by {{"]
for col in table_def.columns:
    if not col.is_array:
        grp_ob_lines.append(f"  {col.name}: order_by")
grp_ob_lines.append("  count: order_by")
for fname, _ in agg_fields:
    grp_ob_lines.append(f"  {fname}: order_by")
grp_ob_lines.append("}")
group_order_by_defs.append("\n".join(grp_ob_lines))
```

Query field (no args — filtering is on the envelope, pagination on `nodes`/`group`):

```python
query_fields.append(
    f"  {t.name}(where: {t.name}_bool_exp): {t.name}Result!"
)
```

**Reserved names**: expand `_RESERVED` in `create_graphql_subapp` to include `{T}Result` and `{T}_group` for each registered table.

### 4. Compiler: `compile_nodes_query`

New function that replaces the current `compile_query` for row data:

```python
def compile_nodes_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    dialect: str = "",
    where: dict | None = None,
    order_by: list[dict] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """Compile the nodes sub-query (row data with ordering/pagination)."""
    # identical body to current compile_query with enhanced WHERE/ORDER BY
```

Keep `compile_query` as a thin alias calling `compile_nodes_query` for backward compatibility with existing tests.

### 5. Compiler: `compile_aggregate_query`

Computes scalar aggregates (`count`, `sum_*`, `avg_*`, etc.) over the filtered set. No pagination — aggregates always span the full filtered set.

```python
_AGG_FUNC_MAP = {
    "count":  func.count,
    "sum":    func.sum,
    "avg":    func.avg,
    "stddev": func.stddev,   # stddev_samp, cross-dialect via func.*
    "var":    func.variance, # var_samp, cross-dialect via func.*
    "min":    func.min,
    "max":    func.max,
}

_NUMERIC_TYPES = {"Int", "Float", "ID"}


def compile_aggregate_query(
    tdef: TableDef,
    requested_agg_fields: set[str],
    registry: TableRegistry,
    where: dict | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """Return a SELECT of only the requested aggregate columns.

    ``requested_agg_fields`` is the set of flat names the caller selected,
    e.g. ``{"count", "sum_Total", "avg_Total"}``. Only those aggregates are
    computed; unselected ones are skipped to keep the query minimal.
    """
    aliased = _build_aliased_table(tdef, registry)
    resolved_policy = resolve_policy(tdef.name) if resolve_policy else None

    projections = []

    if "count" in requested_agg_fields:
        projections.append(func.count().label("count"))

    for col in tdef.columns:
        if col.is_array:
            continue
        t = col.gql_type
        if t in _NUMERIC_TYPES:
            for fn_name in ("sum", "avg", "stddev", "var", "min", "max"):
                field_name = f"{fn_name}_{col.name}"
                if field_name not in requested_agg_fields:
                    continue
                if resolved_policy and col.name in (resolved_policy.blocked_columns or set()):
                    raise ColumnAccessDenied(tdef.name, col.name)
                fn = _AGG_FUNC_MAP[fn_name]
                projections.append(fn(aliased.c[col.name]).label(field_name))
        else:
            # min/max on non-numeric (String)
            for fn_name in ("min", "max"):
                field_name = f"{fn_name}_{col.name}"
                if field_name not in requested_agg_fields:
                    continue
                if resolved_policy and col.name in (resolved_policy.blocked_columns or set()):
                    raise ColumnAccessDenied(tdef.name, col.name)
                fn = _AGG_FUNC_MAP[fn_name]
                projections.append(fn(aliased.c[col.name]).label(field_name))

    if not projections:
        projections = [func.count().label("count")]

    stmt = select(*projections).select_from(aliased)

    if where:
        stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))
    if resolved_policy and resolved_policy.row_filter_clause:
        stmt = stmt.where(resolved_policy.row_filter_clause(aliased))

    return stmt
```

### 6. Compiler: `compile_group_query`

Cube-style GROUP BY: grouping keys are auto-derived from whichever dimension columns appear in `field_nodes`.

```python
def compile_group_query(
    tdef: TableDef,
    field_nodes: list,
    registry: TableRegistry,
    where: dict | None = None,
    order_by: list[dict] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    resolve_policy: ResolvePolicy | None = None,
) -> Select:
    """GROUP BY auto-derived from selected dimension fields (Cube pattern).

    Dimension fields are columns from the table that are not aggregate
    fields (i.e. their name doesn't start with a known aggregate prefix
    like sum_, avg_, etc.). The resolver passes field_nodes for the
    Invoice_group selection set; this function inspects which fields were
    requested and partitions them into dimension vs aggregate.
    """
    aliased = _build_aliased_table(tdef, registry)
    resolved_policy = resolve_policy(tdef.name) if resolve_policy else None

    # Parse requested fields from selection set
    requested = _collect_field_names(field_nodes)  # set[str]
    dim_col_names = {c.name for c in tdef.columns if not c.is_array}
    agg_prefixes = ("count", "sum_", "avg_", "stddev_", "var_", "min_", "max_")

    dimension_fields = [
        f for f in requested
        if f in dim_col_names
        and not any(f.startswith(p) for p in agg_prefixes)
    ]
    aggregate_fields = [f for f in requested if f not in dim_col_names or f == "count"]

    # Build GROUP BY projections
    group_cols = [aliased.c[d] for d in dimension_fields]

    agg_projections = []
    if "count" in aggregate_fields:
        agg_projections.append(func.count().label("count"))

    for col in tdef.columns:
        if col.is_array:
            continue
        t = col.gql_type
        for fn_name, applies_to_strings in (
            ("sum", False), ("avg", False), ("stddev", False), ("var", False),
            ("min", True), ("max", True),
        ):
            field_name = f"{fn_name}_{col.name}"
            if field_name not in requested:
                continue
            if t not in _NUMERIC_TYPES and not applies_to_strings:
                continue
            if resolved_policy and col.name in (resolved_policy.blocked_columns or set()):
                raise ColumnAccessDenied(tdef.name, col.name)
            fn = _AGG_FUNC_MAP[fn_name]
            agg_projections.append(fn(aliased.c[col.name]).label(field_name))

    stmt = select(*group_cols, *agg_projections).select_from(aliased)
    if group_cols:
        stmt = stmt.group_by(*group_cols)

    if where:
        stmt = stmt.where(_where_to_clause(where, aliased, tdef, resolved_policy))
    if resolved_policy and resolved_policy.row_filter_clause:
        stmt = stmt.where(resolved_policy.row_filter_clause(aliased))

    # ORDER BY — flat: dimension columns and aggregate fields at same level
    if order_by:
        for item in order_by:
            for key, direction in item.items():
                order_fn = _ORDER_BY_MAP.get(direction)
                if order_fn is None:
                    raise ValueError(f"Unknown order_by direction: {direction!r}")
                if key == "count":
                    stmt = stmt.order_by(order_fn(literal_column("count")))
                elif key in dim_col_names:
                    stmt = stmt.order_by(order_fn(aliased.c[key]))
                else:
                    # aggregate field like sum_Total, avg_Total
                    stmt = stmt.order_by(order_fn(literal_column(key)))

    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)

    return stmt
```

### 7. SDL Changes in `app.py`

**`_build_ariadne_sdl` rewrite plan**:

Current function builds: `scalar_defs + where_input_defs + type_blocks + [table_info_block, query_block]`.

New function builds:
```
scalar_defs
+ comparison_exp_types   (once: String_comparison_exp, Int_comparison_exp, Float_comparison_exp, Boolean_comparison_exp)
+ order_by_enum          (once: enum order_by { asc asc_nulls_first ... })
+ bool_exp_defs          (per table: {T}_bool_exp)
+ order_by_defs          (per table: {T}_order_by)
+ group_order_by_defs    (per table: {T}_group_order_by)
+ type_blocks            (per table: type {T} { ... } — unchanged)
+ result_type_blocks     (per table: type {T}Result { ... })
+ group_type_blocks      (per table: type {T}_group { ... })
+ [table_info_block, query_block]
```

The `query_block` changes from:
```python
f"  {t.name}(limit: Int, offset: Int, where: {t.name}WhereInput): [{t.name}]"
```
to:
```python
f"  {t.name}(where: {t.name}_bool_exp): {t.name}Result!"
```

### 8. Resolver changes in `resolvers.py`

#### `create_query_type` signature change

Current:
```python
def create_query_type(registry) -> QueryType:
```

New — must also register `ObjectType` bindings for `{T}Result` and `{T}_group`:
```python
def create_query_type(registry) -> tuple[QueryType, list[ObjectType]]:
```

`app.py` then passes both to `make_executable_schema`:
```python
query_type, object_types = create_query_type(registry)
gql_schema = make_executable_schema(_build_ariadne_sdl(registry), query_type, *object_types)
```

#### Root resolver (`{T}`)

Returns a "carrier" dict that holds `where` + context so child resolvers can re-use it. No DB call happens here.

```python
def _make_root_resolver(table_name: str):
    def resolve_root(_, info, where: dict | None = None) -> dict:
        return {"where": where, "table_name": table_name, "info": info}
    return resolve_root
```

#### `nodes` sub-resolver

```python
def _make_nodes_resolver(table_name: str):
    async def resolve_nodes(
        parent: dict,
        info,
        order_by: list[dict] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        db = ctx["db"]
        cache_cfg = ctx["cache_config"]
        jwt_payload = ctx.get("jwt_payload")
        policy_engine = ctx.get("policy_engine")
        resolve_policy = (
            functools.partial(policy_engine.evaluate, ctx=jwt_payload)
            if policy_engine else None
        )
        try:
            stmt = compile_nodes_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=ctx["registry"],
                dialect=db.dialect_name,
                where=parent.get("where"),
                order_by=order_by,
                limit=limit,
                offset=offset,
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc
        try:
            return await execute_with_cache(stmt, dialect_name=db.dialect_name, runner=db.execute, cfg=cache_cfg)
        except SAPoolTimeoutError as exc:
            raise GraphQLError(
                "database connection pool exhausted",
                extensions={"code": POOL_TIMEOUT_CODE, "retry_after": db._pool.retry_after},
            ) from exc
    return resolve_nodes
```

#### `count` + scalar aggregate sub-resolvers

Each aggregate field on `{T}Result` gets its own resolver bound to the `{T}Result` `ObjectType`. To avoid N round-trips, the pattern is **batched**: the first aggregate field requested triggers one `compile_aggregate_query` call; the result is stashed on the parent carrier dict and subsequent sibling field resolvers read from it.

Implementation using a lazy-populate pattern:

```python
_AGG_CACHE_KEY = "__agg_result__"

def _make_aggregate_field_resolver(table_name: str, field_name: str):
    async def resolve_agg_field(parent: dict, info) -> Any:
        # Lazy: compute all aggregates once and cache on parent
        if _AGG_CACHE_KEY not in parent:
            ctx = info.context
            tdef = ctx["registry"].get(table_name)
            # Collect all aggregate fields requested in this selection set
            # by inspecting the parent field's selection set
            result_field_nodes = _get_parent_field_nodes(info)
            requested = {
                f.name.value for f in result_field_nodes
                if f.name.value not in ("nodes", "group")
            }
            # ... (policy setup same as nodes resolver)
            stmt = compile_aggregate_query(
                tdef=tdef,
                requested_agg_fields=requested,
                registry=ctx["registry"],
                where=parent.get("where"),
                resolve_policy=resolve_policy,
            )
            rows = await execute_with_cache(...)
            parent[_AGG_CACHE_KEY] = rows[0] if rows else {}
        return parent[_AGG_CACHE_KEY].get(field_name)
    return resolve_agg_field
```

**Note**: GraphQL resolvers for sibling fields execute concurrently in the same event loop tick for async resolvers. The `asyncio.Lock`-per-parent approach ensures only one DB query fires regardless of concurrency. See implementation note below.

**Simpler alternative** (preferred for v1): resolve `count` and all aggregate fields in a single resolver attached to a sentinel field, and make the rest return from a shared dict. Since Ariadne resolves object fields sequentially within one request, a simple `if _AGG_CACHE_KEY not in parent` check without a lock is safe for the single-threaded async case.

#### `group` sub-resolver

```python
def _make_group_resolver(table_name: str):
    async def resolve_group(
        parent: dict,
        info,
        order_by: list[dict] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        ctx = info.context
        tdef = ctx["registry"].get(table_name)
        # ... (policy setup)
        try:
            stmt = compile_group_query(
                tdef=tdef,
                field_nodes=info.field_nodes,
                registry=ctx["registry"],
                where=parent.get("where"),
                order_by=order_by,
                limit=limit,
                offset=offset,
                resolve_policy=resolve_policy,
            )
        except PolicyError as exc:
            raise _to_graphql_error(exc) from exc
        return await execute_with_cache(...)
    return resolve_group
```

#### Binding all resolvers

```python
def create_query_type(registry) -> tuple[QueryType, list[ObjectType]]:
    query_type = QueryType()
    object_types = []

    for table_def in registry:
        name = table_def.name
        query_type.set_field(name, _make_root_resolver(name))

        result_type = ObjectType(f"{name}Result")
        result_type.set_field("nodes", _make_nodes_resolver(name))
        result_type.set_field("group", _make_group_resolver(name))

        # Aggregate fields
        agg_fields = _agg_field_names_for_table(table_def)  # ["count", "sum_Total", ...]
        for field_name in agg_fields:
            result_type.set_field(field_name, _make_aggregate_field_resolver(name, field_name))

        object_types.append(result_type)

    query_type.set_field("_sdl", _resolve_sdl)
    query_type.set_field("_tables", _resolve_tables)
    return query_type, object_types
```

---

## Implementation Order

All of this ships in one commit. Within that commit, the safe build order is:

1. **`compiler/query.py`**: Add `_where_to_clause`, `_collect_where_columns`, `_ORDER_BY_MAP`, `_compile_order_by`, `_AGG_FUNC_MAP`, `compile_nodes_query`, `compile_aggregate_query`, `compile_group_query`. Keep `compile_query` as a backward-compat alias.

2. **`graphql/app.py`**: Rewrite `_build_ariadne_sdl` to emit the new SDL (result envelope, group types, flat order_by inputs, bool_exp). Update `create_graphql_subapp` to unpack the tuple from `create_query_type`.

3. **`graphql/resolvers.py`**: Rewrite `create_query_type` to return `tuple[QueryType, list[ObjectType]]`. Add all resolver factories.

4. **Tests**: Update existing WHERE/pagination tests for new `bool_exp` syntax. Add tests for aggregates, group queries, and the resolver carrier pattern.

---

## Explicitly Not Included

| Feature | Reason |
|---------|--------|
| `distinct_on` | PostgreSQL `SELECT DISTINCT ON` has no MySQL/Doris equivalent; no dialect-agnostic SQLAlchemy path exists |
| Cursor-based pagination | `limit`/`offset` covers the use case; cursor pagination adds complexity for a pre-1.0 project |
| Subscription support | Out of scope |
| Mutation support | Read-only by design |
| `stddev_pop` / `var_pop` / `stddev_samp` / `var_samp` separately | Exposed as single `stddev` / `var` (stddev_samp / var_samp semantics) to keep the flat field list manageable; population variants can be added later |

---

## Test Plan

### WHERE tests (add to existing `test_query.py`)

```python
# Comparison operators
{"BillingState": {"_eq": "CA"}}
{"Total": {"_gte": 10.0, "_lt": 100.0}}
{"InvoiceId": {"_in": [1, 2, 3]}}
{"Total": {"_is_null": False}}
{"BillingState": {"_like": "C%"}}

# Boolean combinators
{"_and": [{"BillingState": {"_eq": "CA"}}, {"Total": {"_gte": 10.0}}]}
{"_or": [{"BillingState": {"_eq": "CA"}}, {"BillingState": {"_eq": "NY"}}]}
{"_not": {"BillingState": {"_eq": "CA"}}}
{"_and": [{"_or": [...]}, {"_not": {...}}]}  # nested
```

### ORDER BY tests

```python
order_by=[{"Total": "desc"}]
order_by=[{"BillingState": "asc"}, {"Total": "desc"}]
```

### Aggregate tests

```python
# count only
requested = {"count"}

# numeric aggregates
requested = {"count", "sum_Total", "avg_Total", "stddev_Total", "var_Total"}

# min/max on string column
requested = {"min_BillingState", "max_BillingState"}

# combined with WHERE
where = {"BillingState": {"_eq": "CA"}}
requested = {"count", "sum_Total"}
```

### GROUP BY tests

```python
# Single dimension
field_names_in_selection = ["BillingState", "count", "sum_Total"]
# → GROUP BY BillingState

# Multiple dimensions
field_names_in_selection = ["BillingState", "CustomerId", "count"]
# → GROUP BY BillingState, CustomerId

# With WHERE + ORDER BY
where = {"Total": {"_gte": 5.0}}
order_by = [{"sum_Total": "desc"}]

# Empty dimension set (grand total)
field_names_in_selection = ["count", "sum_Total"]
# → no GROUP BY, returns single row
```

### Resolver integration tests

```python
# Root resolver returns carrier dict without hitting DB
# nodes resolver fires compile_nodes_query
# count resolver fires compile_aggregate_query once even when multiple agg fields requested
# group resolver fires compile_group_query with correct GROUP BY columns
```
