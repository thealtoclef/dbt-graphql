# Access Policy

`dbt-graphql` supports per-request access control at the GraphQL layer.
Policies are declared in a single YAML file (`access.yml` by convention) and
evaluated at **SQL compile time** — column restrictions, masking, and row
filters are injected into the generated SQL, so the runtime never sees values
it should not.

This is the **application-level** control plane. Warehouse-level controls
(column policy tags, RLS, IAM on service accounts) remain the warehouse's
responsibility and are complementary. A single dbt model can be served by
multiple applications, each with a different `access.yml`.

---

## Quick start

1. Write an `access.yml` (see [`access.example.yml`](../access.example.yml)
   for a working reference).
2. Reference it from `config.yml`:
   ```yaml
   security:
     policy_path: access.yml
   ```
3. Start the API. Each GraphQL request is evaluated against the policy using
   the `Authorization: Bearer <jwt>` header.

> **Production prerequisite — JWT signature verification.** The JWT
> auth backend uses PyJWT with `verify_signature=False`: it reads the
> payload and exposes it to `when:` / `row_level:` templates, but does
> not verify signatures, algorithms, or standard claims (`exp`, `aud`,
> `iss`). Sec-A tracks the remaining verification work; see
> [security.md](security.md) for the resource-server design the auth
> layer follows, and the [roadmap](../ROADMAP.md) for the verification
> checklist. **Do not expose the API to untrusted networks until Sec-A
> verification lands.**

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
        row_level: "org_id = {{ jwt.claims.org_id }}"
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
| `row_level` | string | Jinja SQL template rendered with bind parameters. |

### `column_level`

| Field | Type | Description |
|---|---|---|
| `include_all` | bool | Allow all columns. Mutually exclusive with `includes`. |
| `includes` | list[str] | Explicit column allow-list. |
| `excludes` | list[str] | Columns to strip from results. |
| `mask` | map[str, str\|null] | `column_name → SQL expression`. Use YAML `~` (null) to emit SQL `NULL`. |

Mask values are raw SQL fragments. `access.yml` is operator-controlled and
trusted; **do not populate it from untrusted sources.**

### `row_level`

A Jinja2 template rendered into a SQL fragment. Every `{{ expression }}` in
the template becomes a SQL bind parameter — the evaluated value is bound via
SQLAlchemy, not interpolated into the SQL string. Write placeholders
unquoted:

```yaml
# Correct
row_level: "user_id = {{ jwt.sub }}"

# Wrong — treats the template as a literal string
row_level: "user_id = '{{ jwt.sub }}'"
```

Jinja conditionals and filters are supported because they run before the
`finalize` hook:

```yaml
row_level: |
  {% if jwt.claims.region %}
  region = {{ jwt.claims.region | upper }}
  {% else %}
  FALSE
  {% endif %}
```

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
     fix the conflict in `access.yml`.
   - **Row filters** — OR: `(filter_a) OR (filter_b)`. Bind params from
     each filter are merged under per-policy prefixes to avoid collisions.
4. Every column the client requested is checked against the merged policy.
   Any column that is not in `allowed_columns` (when set) or is in
   `blocked_columns` triggers `FORBIDDEN_COLUMN` — see
   [strict columns](#strict-columns) below.
5. The compiler uses `ResolvedPolicy` to:
   - Emit the selected columns, replacing masked columns with the mask
     expression.
   - Append `WHERE <row_filter_sql>` using `text(sql).bindparams(**params)`.

### Default-deny

When `access.yml` is loaded, **every table must be explicitly listed under
some policy that matches the subject** — otherwise the request is rejected
with a GraphQL error. This closes the hole where a role with a narrow
policy (say, on `orders`) could silently read any other table the policy
forgot to mention.

When no `access.yml` is configured at all (`security.policy_path` unset),
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

## `row_level` template reference

Templates are rendered in a `jinja2.sandbox.SandboxedEnvironment` with a
`finalize` callback. The sandbox prevents dunder attribute access;
`finalize` replaces every `{{ expression }}` output with a `:param_N`
placeholder and captures the value for binding.

**Bind-param semantics:**

| Template | Rendered SQL | Bind params |
|---|---|---|
| `user_id = {{ jwt.sub }}` | `user_id = :p_0` | `{"p_0": "..."}` |
| `org_id = {{ jwt.claims.org_id | int }}` | `org_id = :p_0` | `{"p_0": 42}` |
| `published = TRUE` | `published = TRUE` | `{}` |
| `{% if jwt.sub %}user = {{ jwt.sub }}{% else %}FALSE{% endif %}` | `user = :p_0` or `FALSE` | conditional |

Because values flow through `SQLAlchemy.text(...).bindparams(...)`, SQL
injection via JWT claims is structurally impossible — even a claim
containing `'; DROP TABLE orders; --` cannot escape its bind slot. This is
covered by a regression test in
[`tests/unit/api/test_policy_integration.py`](../tests/unit/api/test_policy_integration.py).

---

## End-to-end example

```yaml
# access.yml
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
        row_level: "org_id = {{ jwt.claims.org_id }}"

  - name: anon
    when: "jwt.sub == None"
    tables:
      products:
        column_level: { includes: [product_id, name, price] }
        row_level: "published = TRUE"
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
2. **Declarative.** All policy in `access.yml`; no code changes per policy
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

### Structured row-filter DSL (Sec-H)

Replace raw-SQL `row_level` with a JSON/YAML boolean expression tree
(Hasura-style):

```yaml
row_filter:
  all:
    - { col: org_id, eq: jwt.claims.org_id }
    - any:
        - { col: is_public, eq: true }
        - { col: owner_id, eq: jwt.sub }
```

The engine compiles the tree to SQLAlchemy expressions. Column names are
validated against the table registry at load time, so typos are caught
before the first request. Policies become dialect-portable and visually
renderable.

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

- [`access.example.yml`](../access.example.yml) — working example
- [configuration.md](configuration.md) — `security.policy_path` and env vars
- [architecture.md](architecture.md) — where the policy fits in the request path
- [../ROADMAP.md](../ROADMAP.md) — all security phases (Sec-A … Sec-L)
