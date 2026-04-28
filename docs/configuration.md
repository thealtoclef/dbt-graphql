# Configuration Reference

All configuration is loaded from a single YAML file passed via `--config`. A documented template is shipped at [`config.example.yml`](../config.example.yml) — copy it to `config.yml` and edit. Default values for optional fields are defined as constants in [`src/dbt_graphql/defaults.py`](../src/dbt_graphql/defaults.py).

---

## CLI

```
dbt-graphql --config config.yml [--output DIR]
```

| Flag | Description |
|---|---|
| `--config PATH` | Path to `config.yml` (required). |
| `--output DIR` | Write `db.graphql` + `lineage.json` to DIR and exit (generate mode). Omit to serve. |

**Generate mode** (`--output` present): parse dbt artifacts, write schema files, exit. No database connection required.

**Serve mode** (no `--output`): parse dbt artifacts, then start the server. GraphQL is always mounted at `/graphql`. MCP additionally mounts at `/mcp` when `serve.mcp_enabled: true`. Both share one uvicorn process, one JWT auth middleware, and one access policy.

---

## `dbt` (required)

Paths to dbt artifact files produced by `dbt docs generate`.

| Field | Type | Default | Description |
|---|---|---|---|
| `catalog` | Path | — | Path to `target/catalog.json`. Relative paths resolve from the config file's directory. |
| `manifest` | Path | — | Path to `target/manifest.json`. Relative paths resolve from the config file's directory. |
| `exclude` | list | `[]` | Regex patterns matched against model names; matching models are excluded (OR logic). |

---

## `db` (optional)

Database connection — required for serve mode (any time the CLI is invoked without `--output`).

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | string | — | Adapter: `postgres`, `mysql`, `mariadb`, `doris` |
| `host` | string | `""` | Database host |
| `port` | int | `null` | Database port (adapter default if omitted) |
| `dbname` | string | `""` | Database / catalog name |
| `user` | string | `""` | Login user |
| `password` | string | `""` | Login password |
| `pool` | object | (see below) | SQLAlchemy connection-pool tuning |

### `db.pool`

The pool is the admission queue: requests beyond `size + max_overflow` block on checkout, and are fast-failed with `sqlalchemy.exc.TimeoutError` after `timeout` seconds. The API translates that into an HTTP **503 Service Unavailable** with a `Retry-After` header — clean admission denial, not a mid-computation poll.

Set `timeout` **below** your upstream LB idle timeout so the API responds before the LB resets connections.

| Field | Type | Default | Description |
|---|---|---|---|
| `size` | int | `20` | Steady-state connections in the pool |
| `max_overflow` | int | `10` | Burst capacity above `size` (hard cap = `size + max_overflow`) |
| `timeout` | int | `10` | Seconds to wait on checkout before raising. Below LB idle timeout. |
| `recycle` | int | `1800` | Recycle connections after N seconds (NAT/firewall hygiene). `-1` to disable. |
| `retry_after` | int | `5` | Seconds emitted in the `Retry-After` header on 503 responses (per RFC 9110 §10.2.3). Should approximate **p50 warehouse query time**. Distinct from `timeout` (which is admission, not recovery). |

**Sizing rule of thumb:** with `R` replicas and warehouse capacity `Q`, set `size + max_overflow ≤ Q / R`. dbt-graphql doesn't coordinate across replicas — total warehouse concurrency is the sum of per-replica pools.

---

## `serve` (optional)

HTTP server bind config. Required when running in serve mode.

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | — | Bind address (e.g. `0.0.0.0`) |
| `port` | int | — | TCP port |
| `mcp_enabled` | bool | `false` | Mount the MCP server at `/mcp` (Streamable HTTP) in addition to GraphQL. Off by default — opt in to expose schema discovery + `run_graphql` to LLM agents. |
| `graphql_introspection` | bool | `false` | Allow GraphQL schema introspection queries. **Off by default** — production should keep it off so the schema isn't enumerable. Enable in dev for tooling like GraphQL Playground / Apollo Studio. |

GraphQL is always mounted at `/graphql` in serve mode. When `mcp_enabled: true`, the MCP server co-mounts at `/mcp` under the same uvicorn process — sharing the JWT auth middleware, the connection pool, and the access policy. The MCP `run_graphql` tool re-executes queries through the same engine, so column allow-lists, masks, and row filters apply uniformly to both transports.

### Operating the pool admission 503

When the warehouse is overloaded, the connection pool reaches capacity (`size + max_overflow`). New requests then wait up to `db.pool.timeout` seconds and, if no connection becomes available, fail with **HTTP 503** plus a `Retry-After: <db.pool.retry_after>` header. The LB sees this as clean admission denial and can retry without holding the upstream socket open.

| Operator concern | Where to look |
|---|---|
| **"Are we hitting the pool ceiling?"** | OTel histogram `db.client.connections.wait_time` — p95/p99 climbing toward `db.pool.timeout` is the leading indicator. |
| **"How often are we returning 503?"** | Resolver emits a GraphQL error with `extensions.code = "POOL_TIMEOUT"`; the HTTP layer elevates that to 503. Track 5xx rate per route. |
| **When to widen the pool** | Sustained `wait_time` p95 > 1s with available warehouse headroom → bump `db.pool.size` (steady) or `db.pool.max_overflow` (burst). Sizing rule above caps the total. |
| **When `Retry-After` is too short / long** | Aim for the warehouse's p50 query time. Too short and the LB hammers a saturated backend; too long and the client experiences a stall. |
| **When 503 is *not* the right answer** | If the warehouse itself is healthy and we're 503-ing on stuck connections, raise `db.pool.recycle` complaints — connections are being recycled mid-flight or held by hung queries. |

---

## `graphql` (optional)

Query guard limits applied to all incoming GraphQL operations — both HTTP `/graphql` and MCP `run_graphql`. Guards are checked *before* the query is executed; exceeding a limit returns a 400 response (HTTP) or an error dict (MCP) without touching the database.

| Field | Type | Default | Description |
|---|---|---|---|
| `query_max_depth` | int | `5` | Maximum selection-set nesting depth. Introspection-only queries (`__schema { ... }`) are excluded from this limit. |
| `query_max_fields` | int | `50` | Maximum total leaf fields across the entire query. |
| `query_max_limit` | int \| null | `1000` | Caps integer literals on `limit:` / `first:` arguments. `null` disables the cap. Variables are not checked at validation time — resolvers must apply runtime caps when accepting variables for pagination. Emits `MAX_LIMIT_EXCEEDED` on violation. |

The default values (5 levels of nesting, 50 leaf fields) follow Hasura's defaults and cover typical analytics queries (5–10 tables × 5–10 fields each). Apollo Router defaults to 100/200 but is geared toward enterprise multi-tenant APIs.

When a limit is exceeded, the HTTP path returns **400 Bad Request** with a GraphQL error body:

```json
{
  "data": {},
  "errors": [{
    "message": "Query depth 12 exceeds the limit of 5",
    "extensions": {"code": "MAX_DEPTH_EXCEEDED"}
  }]
}
```

The MCP path returns the same error messages in the tool result dict:

```python
{"errors": [{"message": "Query depth 12 exceeds the limit of 5"}]}
```

---

## `monitoring` (optional)

OpenTelemetry configuration and log level. Omit the block (or any sub-block) to use defaults from [`defaults.py`](../src/dbt_graphql/defaults.py). Signals are configured independently — you can ship only traces, only logs, or any combination.

### `monitoring.logs`

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | string | `"INFO"` | Log level: `trace`, `debug`, `info`, `warning`, `error`, `critical` |
| `endpoint` | string | `null` | OTLP collector URL. When set, log records are shipped via OTLP in addition to the console. |
| `protocol` | string | `null` | OTLP transport: `grpc` or `http`. **Required when `endpoint` is set.** |

Console (stderr) output is always active regardless of whether an OTLP endpoint is configured. Console span export is enabled automatically when `level` is `trace` or `debug`.

### `monitoring.traces`

| Field | Type | Default | Description |
|---|---|---|---|
| `endpoint` | string | `null` | OTLP collector URL for spans. |
| `protocol` | string | `null` | OTLP transport: `grpc` or `http`. **Required when `endpoint` is set.** |

### `monitoring.metrics`

| Field | Type | Default | Description |
|---|---|---|---|
| `endpoint` | string | `null` | OTLP collector URL for metrics. |
| `protocol` | string | `null` | OTLP transport: `grpc` or `http`. **Required when `endpoint` is set.** |

### Top-level monitoring fields

| Field | Type | Default | Description |
|---|---|---|---|
| `service_name` | string | `"dbt-graphql"` | OTel `service.name` resource attribute |

Setting `endpoint` without `protocol` raises a config error at startup.

---

## `cache` (optional)

Result cache + singleflight, sitting between the resolver and the warehouse. See
[caching.md](caching.md) for the key-derivation argument and tenant-isolation
proof.

Omit the block to use the default in-memory cache. Pass `cache_config=None` programmatically to `create_app()` to disable caching entirely — useful for tests measuring an uncached baseline.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Disable to bypass the cache entirely (no caching, no coalescing). |
| `url` | string | `"mem://?size=10000"` | [cashews](https://github.com/Krukov/cashews) URI. Examples: `mem://?size=N`, `redis://host:6379/0`, `redis://...?cluster=true`. Use a Redis URI for multi-replica deployments — both the cache and the singleflight lock then live on the shared backend, so coalescing crosses replicas. Redis requires `pip install dbt-graphql[redis]`. |
| `ttl` | int | `60` | Freshness window in seconds. `0` = realtime + 1 s coalescing window; see caching.md. |
| `lock_safety_timeout` | int | `10` | Singleflight lock auto-release, in seconds. Set **above your p99 warehouse query time** and **below your LB idle timeout**. Default targets fast warehouses (p99 ≤ 5s) behind a 30s LB. Bump to 25–60 for slower workloads. **Not** the result TTL. |

---

## `security` (optional)

Single master switch for JWT verification (authn) and access policies
(authz). See [access-policy.md](access-policy.md) for the policy
language and [security.md](security.md) for the auth model.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. `false` ⇒ no JWT verification, no policy evaluation; every request is anonymous (the server warns at startup). `true` ⇒ JWT block must be configured and policies (if any) are enforced. |
| `jwt` | object | `{}` | JWT verification settings (see below). Only consulted when `enabled: true`. |
| `policies` | list | `[]` | Inline access policies — same shape as the [access-policy.md](access-policy.md) DSL. Empty list = no row/column enforcement (authn-only) when security is enabled. |

Tying authn and authz to one switch prevents the "JWT off but policies
armed against `jwt.*` claims" misconfiguration where every request would
silently match the most-permissive rule.

### `security.jwt`

Consulted only when `security.enabled: true`. When the master switch is
true, every request must present a valid `Bearer` token or it is
rejected with HTTP 401 + `WWW-Authenticate: Bearer
error="invalid_token"`. Exactly one of `jwks_url`, `key_url`, `key_env`,
or `key_file` must be set.

| Field | Type | Default | Description |
|---|---|---|---|
| `algorithms` | list | `[]` | Required when `security.enabled: true`. Allow-list of accepted JWS algorithms (e.g. `[RS256]`, `[HS256]`). Pinned — `none` and unlisted algorithms are rejected before signature checks run. |
| `audience` | str \| list | `null` | If set, token's `aud` claim must equal (str) or be a member of (list) this value. |
| `issuer` | str | `null` | If set, token's `iss` claim must equal this value. |
| `leeway` | int | `30` | Clock-skew tolerance in seconds for `exp` / `nbf` / `iat`. |
| `required_claims` | list | `["exp"]` | Claims that must be present. |
| `roles_claim` | str | `"scope"` | Claim read for Starlette scopes. Space-delimited string or list. Set to `scp`, `roles`, or a namespaced URL for non-OIDC IdPs. |
| `jwks_url` | URL | `null` | Rotating JWKS endpoint (RS256/ES256). The keyset is cached for `jwks_cache_ttl` seconds and refetched on TTL expiry; concurrent refetches are coalesced. JWKS-fetch failure produces 401, not stale keys. |
| `jwks_cache_ttl` | int | `3600` | TTL for the in-memory JWKS cache. Only meaningful with `jwks_url`. |
| `key_url` | URL | `null` | URL of a single static key (PEM or JWK). Fetched once on first request. |
| `key_env` | str | `null` | **Name** of an environment variable holding the key material (HMAC secret, PEM, or JWK). Not the secret itself. |
| `key_file` | Path | `null` | Path to a single key file (PEM or JWK). |

---

## Environment variables

All config fields can be overridden via `DBT_GRAPHQL__` prefixed env vars. Nested fields use `__` as delimiter.

```
DBT_GRAPHQL__DB__HOST=myhost
DBT_GRAPHQL__DB__PASSWORD=secret
DBT_GRAPHQL__MONITORING__LOGS__LEVEL=DEBUG
DBT_GRAPHQL__MONITORING__TRACES__ENDPOINT=http://collector:4317
DBT_GRAPHQL__MONITORING__TRACES__PROTOCOL=grpc
```

### Precedence

Sources are layered in this order (later wins):

1. **Defaults** — values declared on `AppConfig` / its sub-models in [`src/dbt_graphql/config.py`](../src/dbt_graphql/config.py).
2. **Config file** — values in `config.yml` passed via `--config`.
3. **Environment variables** — `DBT_GRAPHQL__*` overrides.

So **env > file > defaults**. The order is enforced by `AppConfig.settings_customise_sources` in `config.py` (Pydantic-Settings).

#### Worked example

```yaml
# config.yml
db:
  host: file-host
```

```bash
DBT_GRAPHQL__DB__HOST=env-host dbt-graphql --config config.yml
```

Effective `db.host` is `env-host`. The env var wins; the file value is shadowed; the in-code default is shadowed by the file value before that.
