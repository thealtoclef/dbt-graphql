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
```

Top-level key is `policies` (a list). Each entry has:

| Field | Type | Description |
|---|---|---|
| `name` | string | Human-readable label (used in logs). |
| `when` | string | Boolean expression evaluated by `simpleeval` against `jwt`. |
| `tables` | map | Per-table rules keyed by GraphQL table name. |

Each table entry contains:

| Field | Type | Description |
|---|---|---|
| `column_level` | object | Column allow/deny + masking. |
| `row_filter` | object | Structured boolean-expression DSL (Hasura-style). |

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

A structured boolean-expression tree (Hasura convention). The engine
compiles it to a SQLAlchemy `ColumnElement` — column references are
validated at policy-load time, JWT values are bound as named parameters,
and there is no template engine in the data-access path.

**Logical operators:** `_and`, `_or`, `_not`. **Comparison operators:**
`_eq`, `_ne`, `_lt`, `_lte`, `_gt`, `_gte`, `_in`, `_is_null`. RHS values
are either:

- a literal scalar (`str`, `int`, `float`, `bool`),
- a non-empty list of literals (for `_in`), or
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
    when: "'data-admins' in jwt.groups"
    tables:
      orders:    { column_level: { include_all: true } }
      customers: { column_level: { include_all: true } }

  - name: analyst
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
    when: "jwt.sub == None"
    tables:
      products:
        column_level: { includes: [product_id, name, price] }
        row_filter:
          published: { _eq: true }
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

Moving to structured `match:` blocks (instead of opaque string expressions)
makes policies statically inspectable — you can answer *"which policies
apply to this JWT?"* without actually evaluating a request.

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

### Deny rules (Sec-G)

OR semantics cannot express *"contractors never see salary, even if they're
also analysts."* First-class `deny:` with highest precedence:

```yaml
- name: contractor_deny
  when: "'contractors' in jwt.groups"
  deny:
    customers: [salary, ssn]
```

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
