# Access Policy

`dbt-graphql` supports per-request access control at the GraphQL layer.
Policies are declared **inline in `config.yml`** under `security.policies`
and evaluated at **SQL compile time** — column restrictions, masking, and
row filters are injected into the generated SQL, so the runtime never sees
values it should not.

This is the **application-level** control plane. Warehouse-level controls
(column policy tags, RLS, IAM on service accounts) remain the warehouse's
responsibility and are complementary. A single dbt model can be served by
multiple applications, each with different `security.policies`.

---

## Quick start

Declare policies inline in `config.yml`:

```yaml
security:
  enabled: true             # master switch — JWT verified, policies enforced
  jwt:
    algorithms: [RS256]
    jwks_url: https://issuer.example/.well-known/jwks.json
  policies:
    - name: analyst
      effect: allow
      when: "'analysts' in jwt.groups"
      tables:
        customers:
          column_level: { include_all: true }
```

Each GraphQL **and MCP** request is evaluated against the policy using the
`Authorization: Bearer <jwt>` header. Both transports share the same
`AuthenticationMiddleware` and the same `AccessPolicy`, so a caller's
column allow-list, masks, and row filters apply identically to direct
`/graphql` calls and to MCP tools (`list_tables`, `describe_table`,
`run_graphql`, …). See [mcp.md § Authorization model](mcp.md#authorization-model).

When `security.enabled` is false (the default), JWTs are not verified and
policies are not evaluated — every request is anonymous. The server warns
at startup. Use this for dev or behind a trusted proxy that authenticates
upstream.

---

## Policy file structure

```yaml
policies:
  - name: analyst
    effect: allow
    when: "'analysts' in jwt.groups"
    tables:
      customers:
        column_level:
          include_all: true
          excludes: [raw_source_id]
          mask:
            email: "CONCAT('***@', SPLIT_PART(email, '@', 2))"
            ssn: ~
        row_filter:
          org_id: { _eq: { jwt: claims.org_id } }

  # Cross-cutting deny rule. Precedence over every allow above.
  - name: contractors_no_pii
    effect: deny
    when: "'contractors' in jwt.groups"
    tables:
      customers: { deny_columns: [email, ssn] }
      orders:    { deny_all: true }
```

Top-level key is `policies` (a list). Each entry has:

| Field | Type | Description |
|---|---|---|
| `name` | string | Human-readable label (used in logs). |
| `effect` | enum | `allow` or `deny`. **Required**, no default. See [`effect`](#effect) below. |
| `when` | string | Boolean expression evaluated by `simpleeval` against `jwt`. |
| `tables` | map | Per-table rules keyed by GraphQL table name. |

Each table entry contains a different set of fields depending on the
parent entry's `effect`:

| Effect | Field | Type | Description |
|---|---|---|---|
| `allow` | `column_level` | object | Column allow-list + masking. |
| `allow` | `row_filter` | object | Structured boolean-expression DSL (Hasura-style). |
| `deny`  | `deny_all` | bool | Deny the whole table for any subject the rule matches. |
| `deny`  | `deny_columns` | list[str] | Deny specific columns. |

Mixing fields across effects (e.g. `column_level` under a `deny` rule)
fails policy load — the loader names the offending entry and table.

### `effect`

The `effect` field follows the
[XACML](https://en.wikipedia.org/wiki/XACML) / [AWS IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_effect.html)
convention — the standard vocabulary for ABAC policies, where every rule
has a single `Effect` of either `Allow` or `Deny`. Cedar uses the
synonym pair `permit` / `forbid`; SQL Server uses `GRANT` / `DENY`.
The semantics are the same across all three: **a deny always wins over
an allow**.

`effect` is required on every entry — there is no implicit default.
Forcing the operator to type `effect: allow` on every grant rule keeps
the file readable and prevents the "I forgot which kind this was"
mistake that an `effect: allow` default would invite.

**Allow rules** are additive. The merged result of every matching allow
rule produces the per-table column allow-list, mask map, and row-filter
predicate.

**Deny rules** are subtractive and take precedence:

- `deny_all: true` for a matching subject → the table is denied with
  `FORBIDDEN_TABLE`, regardless of any allow rule that would have
  granted access.
- `deny_columns: [...]` for a matching subject → those columns are
  removed from the merged allow result, added to the blocked set, and
  any mask the allow side declared for them is dropped (masking a
  blocked column is meaningless). Selecting a denied column triggers
  `FORBIDDEN_COLUMN`.

A request with **only matching denies and no matching allow** still
falls through to default-deny — denies subtract from grants; they don't
imply one.

The cross-cutting use case `effect: deny` exists to solve:

> *"contractors never see `salary`, even when they're also in the
> `analysts` group."*

Without deny, that guard has to be copied into every allow rule's
`when` clause and any allow added later that forgets the guard silently
re-grants the column. With `effect: deny`, the rule lives in one place
and a new allow cannot accidentally bypass it.

### `column_level`

| Field | Type | Description |
|---|---|---|
| `include_all` | bool | Allow all columns. Mutually exclusive with `includes`. |
| `includes` | list[str] | Explicit column allow-list. |
| `excludes` | list[str] | Columns to strip from results. |
| `mask` | map[str, str\|null] | `column_name → SQL expression`. Use YAML `~` (null) to emit SQL `NULL`. |

Mask values are raw SQL fragments. The `security.policies` block is operator-controlled and
trusted; **do not populate it from untrusted sources.**

### `row_filter`

A structured boolean-expression tree. The operator vocabulary
(`_eq`, `_neq`, `_lt`, `_lte`, `_gt`, `_gte`, `_in`, `_nin`, `_is_null`,
`_like`, `_nlike`, `_ilike`, `_nilike`, `_and`, `_or`, `_not`) is taken directly from
[Hasura's permission-rule DSL](https://hasura.io/docs/latest/auth/authorization/permissions/row-level-permissions/),
which is the de-facto standard for declarative row-level filters in the
GraphQL data-API ecosystem (PostGraphile, GraphJin, and Postgrest all
use the same shape). Adopting it means operators familiar with any of
those tools recognize the syntax immediately, and we get a vocabulary
that has already been battle-tested against SQL/NULL edge cases.

The engine compiles the tree to a SQLAlchemy `ColumnElement` — column
references are validated at policy-load time, JWT values are bound as
named parameters, and there is no template engine in the data-access
path.

**Logical operators:** `_and`, `_or`, `_not`. **Comparison operators:**
`_eq`, `_neq`, `_lt`, `_lte`, `_gt`, `_gte`, `_in`, `_nin`, `_is_null`,
`_like`, `_nlike`, `_ilike`, `_nilike`. The same operator vocabulary is
shared with the GraphQL `{T}_bool_exp` filter — both call sites dispatch
through `dbt_graphql.schema.operators.apply_comparison`, the single source of
truth for Hasura-vocab → SQLAlchemy translation. RHS values are either:

- a literal scalar (`str`, `int`, `float`, `bool`),
- a non-empty list of literals (for `_in` / `_nin`), or
- a JWT reference: `{ jwt: <dotted.path> }`, resolved per request.

```yaml
row_filter:
  _and:
    - org_id: { _eq: { jwt: claims.org_id } }    # JWT-driven (per request)
    - _or:
        - is_public: { _eq: true }                # static literal
        - owner_id: { _eq: { jwt: sub } }
    - status: { _in: [active, pending] }          # static list
```

> **YAML note.** The `{ key: value }` form above is YAML *flow style*,
> not JSON — it parses to the same `dict` as the equivalent block style:
>
> ```yaml
> org_id:
>   _eq:
>     jwt: claims.org_id
> ```
>
> Flow style is shipped in the examples because row-filter trees nest
> deeply and block style chews up vertical space. Either is valid.

A missing JWT path resolves to SQL `NULL` — the comparison then yields
`UNKNOWN` and excludes the row, which is the safe default.

A node may not mix logical operators (`_and` / `_or` / `_not`) with
column keys at the same level: the policy loader rejects shapes like
`{ _and: [...], org_id: {...} }`. Wrap the column key inside the `_and`
explicitly to keep the intended semantics visible.

---

## Evaluation model

For each table the query touches — root *and* any table reached by a nested
relation — the engine runs these steps:

1. Every `policies[*].when` expression is evaluated against the request's
   JWT payload.
2. Policies whose `when` is true **and** whose `tables` contains the current
   table are collected. If none match, the request is **denied**
   (`FORBIDDEN_TABLE`) — see [default-deny](#default-deny) below.
3. The collected table-policies are merged into a single `ResolvedPolicy`:
   - **Column access** — OR / union: the most permissive matching policy
     wins. If any matching policy has `include_all: true`, all columns are
     allowed; otherwise the union of `includes` is taken.
   - **Blocked columns** — intersection: a column is blocked only when
     **every** matching policy blocks it.
   - **Masks** — a mask is applied only when every matching policy masks
     the column *and* they agree on the expression. Conflicting expressions
     raise; any matching policy that leaves a column unmasked drops the
     mask (most-permissive rule). Raise at request time forces operators to
     fix the conflict in `security.policies`.
   - **Row filters** — OR: `(filter_a) OR (filter_b)`. Bind params from
     each filter are merged under per-policy prefixes to avoid collisions.
4. Every column the client requested is checked against the merged policy.
   Any column that is not in `allowed_columns` (when set) or is in
   `blocked_columns` triggers `FORBIDDEN_COLUMN` — see
   [strict columns](#strict-columns) below.
5. The compiler uses `ResolvedPolicy` to:
   - Emit the selected columns, replacing masked columns with the mask
     expression.
   - Append the row filter as a SQLAlchemy `ColumnElement` directly into
     `stmt.where(...)`.

### Default-deny

When `security.policies` is loaded, **every table must be explicitly listed under
some policy that matches the subject** — otherwise the request is rejected
with a GraphQL error. This closes the hole where a role with a narrow
policy (say, on `orders`) could silently read any other table the policy
forgot to mention.

When `security.policies` is empty (or `security.enabled` is false),
enforcement is skipped entirely — this is dev/test mode. Pick one:
configure a policy file, or don't; there is no "partial enforcement".

### Strict columns

If the client selects a column that the merged policy does not allow —
either it's outside `includes`, or it's listed in `excludes` — the request
is rejected with `FORBIDDEN_COLUMN` naming the table and the unauthorized
columns. Columns are never silently stripped from the response. Silent
strip creates ambiguous nulls (was this row NULL, or was the column
blocked?) and makes it hard to debug why a query "didn't return the field I
asked for" — strict is always louder, and always clearer.

### Nested relations

Default-deny, strict columns, masks, and row filters all apply the same way
to every table the query reaches — not just the root. Selecting
`customers { orders { internal_notes } }` evaluates the policy for `orders`
just as if `orders` were queried directly. Without this, nested selections
would be a blanket bypass of the whole policy engine.

---

## Error responses

Policy denials surface as standard GraphQL errors (HTTP 200 with a populated
`errors` array — the idiomatic GraphQL convention). The `extensions` block
carries a stable `code` plus structured context:

```json
{
  "errors": [
    {
      "message": "access denied: columns [first_name, last_name] on table 'customers' are not authorized by policy",
      "extensions": {
        "code": "FORBIDDEN_COLUMN",
        "table": "customers",
        "columns": ["first_name", "last_name"]
      }
    }
  ],
  "data": null
}
```

| `code` | When | Extensions |
|---|---|---|
| `FORBIDDEN_TABLE` | No matching policy covers the requested table. | `table` |
| `FORBIDDEN_COLUMN` | Query selects columns not authorized by the merged policy. | `table`, `columns` |

---

## `when` expression reference

`when` is evaluated by [`simpleeval`](https://pypi.org/project/simpleeval/) —
an AST-based Python subset that rejects dunder attribute access (`__class__`,
`__globals__`, ...) and Python builtins (`open`, `exec`, ...). Evaluation
that raises logs a warning and returns `False` (the policy does not match).

Available operators: `and`, `or`, `not`, `in`, `not in`, `==`, `!=`, `<`,
`<=`, `>`, `>=`, arithmetic.

Available JWT fields (via `jwt.<name>`):

- `jwt.sub` — subject
- `jwt.email` — email claim
- `jwt.groups` — list of groups
- `jwt.claims.<key>` — arbitrary nested claims

Missing attributes resolve to `None` rather than raising, so typos silently
evaluate to `False` (they also produce a warning log).

**Examples:**

```yaml
when: "'data-admins' in jwt.groups"
when: "'finance' in jwt.groups and jwt.claims.level >= 3"
when: "jwt.sub == None"                           # anonymous
when: "'eu-west' in jwt.claims.allowed_regions"
```

---

## `row_filter` reference

The grammar follows
[Hasura's row-level permission syntax](https://hasura.io/docs/latest/auth/authorization/permissions/row-level-permissions/)
— the same convention used by PostGraphile, GraphJin, and Postgrest
for declarative SQL filters. Operators carry the same meaning across
those tools.

Each tree node is either a logical-operator map (`_and`, `_or`, `_not`)
or a column-comparison map. Logical operators take a list of sub-trees
(except `_not`, which takes a single sub-tree). A column-comparison map
has a single key (the column name) whose value is a single-key map naming
the comparison operator.

| DSL | Compiled SQL | Notes |
|---|---|---|
| `org_id: { _eq: 7 }` | `org_id = :p_0` (bound) | literal |
| `org_id: { _eq: { jwt: claims.org_id } }` | `org_id = :p_0` (bound) | JWT ref |
| `status: { _in: [active, pending] }` | `status IN (:p_0, :p_1)` | each element bound |
| `owner_id: { _is_null: true }` | `owner_id IS NULL` | no bind |
| `_not: { status: { _eq: deleted } }` | `status != :p_0` | SA collapses |
| `name: { _like: "Acme%" }` | `name LIKE :p_0` | bound; case-sensitive |
| `name: { _ilike: "acme%" }` | `name ILIKE :p_0` | case-insensitive (PG); MySQL falls back to LIKE |

Because values are bound via SQLAlchemy `bindparam`, SQL injection via
JWT claims is structurally impossible — even a claim containing
`'; DROP TABLE orders; --` cannot escape its bind slot. This is covered
by a regression test in
[`tests/unit/graphql/test_policy_integration.py`](../tests/unit/graphql/test_policy_integration.py).

---

## End-to-end example

```yaml
# config.yml — security.policies
policies:
  - name: admin
    effect: allow
    when: "'data-admins' in jwt.groups"
    tables:
      orders:    { column_level: { include_all: true } }
      customers: { column_level: { include_all: true } }

  - name: analyst
    effect: allow
    when: "'analysts' in jwt.groups"
    tables:
      customers:
        column_level:
          include_all: true
          excludes: [raw_source_id]
          mask:
            email: "CONCAT('***@', SPLIT_PART(email, '@', 2))"
            ssn: ~
        row_filter:
          org_id: { _eq: { jwt: claims.org_id } }

  - name: anon
    effect: allow
    when: "jwt.sub == None"
    tables:
      products:
        column_level: { includes: [product_id, name, price] }
        row_filter:
          published: { _eq: true }

  # Deny precedence: contractors lose PII access on customers no matter
  # which other groups also match. Single source of truth.
  - name: contractors_no_pii
    effect: deny
    when: "'contractors' in jwt.groups"
    tables:
      customers: { deny_columns: [email, ssn] }
```

A request `GET /graphql?...` with

```
Authorization: Bearer <jwt with groups=["analysts"], claims.org_id=7>
```

selecting `customers { customer_id email ssn raw_source_id }` compiles to
roughly (postgresql dialect shown):

```sql
SELECT
    _parent.customer_id AS customer_id,
    CONCAT('***@', SPLIT_PART(email, '@', 2)) AS email,
    NULL AS ssn
FROM main.customers AS _parent
WHERE (org_id = :p0_0)
-- bind params: p0_0 = 7
```

`raw_source_id` is stripped entirely; `email` is replaced with the mask
expression; `ssn` is bound `NULL`; the row filter appears as a bound
predicate.

---

## Design principles

1. **Compile-time enforcement.** Filters and masks are injected into the SQL
   sent to the warehouse. The runtime never sees values it should not. It
   is impossible to "forget" to apply a filter downstream.
2. **Declarative.** All policy in `security.policies`; no code changes per policy
   edit.
3. **Parameterized everywhere.** Claim values are never string-interpolated
   into SQL.
4. **Most-permissive OR semantics.** Matches Cube.dev and GraphJin; users
   with multiple applicable roles get the union.
5. **Fail safe on conflict.** Mask conflicts raise rather than pick an
   arbitrary expression.
6. **Default-deny, strict-column, loud.** Tables the policy doesn't cover
   raise `FORBIDDEN_TABLE`. Unauthorized columns raise `FORBIDDEN_COLUMN`.
   The API never returns a partial result that silently hides denials.
7. **Policy applies to every table the query reaches.** Root tables and
   nested-relation tables are evaluated the same way — no bypass via
   nesting.
8. **Application layer, not warehouse layer.** A single dbt model can be
   served by many applications with different policies; warehouse-side
   controls (BigQuery policy tags, Postgres RLS) remain independent.

---

## Enhancement roadmap — SOTA positioning

The current engine covers the core of the Cube / GraphJin feature set. The
industry SOTA is a **PDP/PEP architecture with ABAC-style declarative rules
and a decision log** (see OPA, Cedar, SpiceDB, Cube, Hasura). These
enhancements are tracked in [ROADMAP.md](../ROADMAP.md) under `Sec-B` →
`Sec-L`; headline items:

### ABAC attribute model (Sec-G)

Today's model is implicitly role-based: `when` inspects JWT groups. SOTA
policy engines are **attribute-based** — decisions are functions of four
attribute sets:

| Attribute set | Examples |
|---|---|
| **Subject** | `jwt.sub`, `jwt.groups`, `jwt.claims.*` |
| **Action** | `read`, `aggregate`, `export` |
| **Resource** | table name, column name, column classification (`pii`, `financial`, `public`) |
| **Environment** | request IP, time of day, is-production-hours, rate-limit bucket |

### Column classifications (Sec-I)

Declare sensitivity classes; roles opt in or bypass by class rather than
by column:

```yaml
classifications:
  pii:
    mask: "CONCAT('***@', SPLIT_PART(%s, '@', 2))"   # %s = column
  pii_strict:
    mask: "NULL"

roles:
  analyst:
    respects: [pii]
  compliance:
    respects: [pii, pii_strict]
  admin:
    respects: []
```

One mask change covers every column tagged with that classification.

### External decision point (Sec-J)

Make `dbt-graphql` a **PEP** (Policy Enforcement Point) backed by an
optional external **PDP** (Policy Decision Point):

```yaml
security:
  pdp:
    url: "http://opa:8181/v1/data/dbt_graphql/allow"
    timeout_ms: 50
    cache_ttl_s: 5
```

At request time the engine sends a decision request (subject + action +
resource attributes) to OPA/Cedar/custom; the PDP returns
`{ allowed, filters, masks }`. This is the architecture Stripe, Netflix,
Goldman, and every Kubernetes cluster use for authz. When no `pdp` is
configured, the built-in engine decides locally.

### Hot reload, policy test harness, query allow-list, decision log

All tracked as individual roadmap entries. Each is a single-digit-file
change, but collectively they transform the engine from "policy file" to
"authz platform".

---

## Related documents

- [`config.example.yml`](../config.example.yml) — see the `security:` block
- [configuration.md](configuration.md) — `security.enabled`, `security.policies`, env vars
- [architecture.md](architecture.md) — where the policy fits in the request path
- [../ROADMAP.md](../ROADMAP.md) — all security phases (Sec-A … Sec-L)
