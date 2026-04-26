# Configuration Reference

All configuration is loaded from a single YAML file passed via `--config`. A documented template is shipped at [`config.example.yml`](../config.example.yml) — copy it to `config.yml` and edit. Default values for optional fields are defined as constants in [`src/dbt_graphql/defaults.py`](../src/dbt_graphql/defaults.py).

---

## `db` (required)

Database connection. The `type` field selects the adapter; remaining fields vary by adapter.

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | string | — | Adapter: `postgres`, `mysql`, `mariadb`, `doris` |
| `host` | string | `""` | Database host |
| `port` | int | `null` | Database port (adapter default if omitted) |
| `dbname` | string | `""` | Database / catalog name |
| `user` | string | `""` | Login user |
| `password` | string | `""` | Login password |

---

## `serve` (required for `--target api`)

HTTP server bind config.

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | — | Bind address (e.g. `0.0.0.0`) |
| `port` | int | — | TCP port |

---

## `enrichment` (optional)

Controls live DB queries issued by `describe_table` in the MCP server. Defaults are defined in [`defaults.py`](../src/dbt_graphql/defaults.py).

| Field | Type | Default | Description |
|---|---|---|---|
| `budget` | int | `20` | Max live DB queries fired per `describe_table` call. Row count and sample rows are excluded from this count; budget applies to per-column value enrichment only. |
| `distinct_values_limit` | int | `50` | Max values returned in a `distinct` value summary. |
| `distinct_values_max_cardinality` | int | `500` | If a column's distinct count exceeds this, skip the distinct summary entirely. |

Any field can be overridden at runtime via env var without editing the config file. See the **Environment variables** section below.

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

Omit the block to use the default in-memory result cache. Pass `cache_config=None` programmatically to `create_app()` to disable caching entirely — useful for tests measuring an uncached baseline.

### `cache.backends`

List of [cashews](https://github.com/Krukov/cashews) backends. Each entry is one `cache.setup(url, prefix=...)` call. Empty `prefix` is the catch-all backend; non-empty prefixes route any key starting with that string to this backend.

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | `"mem://?size=10000"` | cashews URI. Examples: `mem://?size=N`, `redis://host:6379/0`, `redis://...?cluster=true`. |
| `prefix` | string | `""` | Route keys with this prefix to this backend. Empty = catch-all. |
| `enabled` | bool | `true` | Skip this backend without removing it from the file. |

For multi-replica deployments, point `backends` at Redis so all replicas share the result cache; the singleflight lock then coalesces bursts cluster-wide rather than per-replica.

### `cache.result`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Disable to bypass the cache entirely (no caching, no coalescing). |
| `default_ttl_s` | int | `60` | TTL applied to any table not in `per_table_ttl_s`. |
| `per_table_ttl_s` | dict[str, int] | `{}` | Per-table TTL overrides. `0` = realtime + 1 s coalescing window; see caching.md. The strictest TTL across all tables touched by a query wins. |
| `lock_safety_timeout_s` | int | `60` | Singleflight lock auto-release. Set above the slowest plausible warehouse query. **Not** the result TTL. |

---

## `security` (optional)

Path to the access-policy file that governs column/row visibility at request
time. See [access-policy.md](access-policy.md) for the policy language and
[security.md](security.md) for the JWT auth model.

| Field | Type | Default | Description |
|---|---|---|---|
| `policy_path` | Path | `null` | Path to `access.yml`. Omit to serve the API with no access policy (all columns/rows visible). |

---

## Environment variables

All config fields can be overridden via `DBT_GRAPHQL__` prefixed env vars. Nested fields use `__` as delimiter.

```
DBT_GRAPHQL__DB__HOST=myhost
DBT_GRAPHQL__DB__PASSWORD=secret
DBT_GRAPHQL__ENRICHMENT__BUDGET=5
DBT_GRAPHQL__MONITORING__LOGS__LEVEL=DEBUG
DBT_GRAPHQL__MONITORING__TRACES__ENDPOINT=http://collector:4317
DBT_GRAPHQL__MONITORING__TRACES__PROTOCOL=grpc
```

Env vars take precedence over values in `config.yml`.

---

## CLI flags (MCP serve)

These flags are passed to `dbt-graphql serve --target mcp`:

| Flag | Description |
|---|---|
| `--config PATH` | Path to `config.yml`. Optional for MCP; required for API. |
| `--catalog PATH` | Path to `catalog.json` (required). |
| `--manifest PATH` | Path to `manifest.json` (required). |
| `--exclude PATTERN` | Regex to exclude models by name. Repeatable. |
