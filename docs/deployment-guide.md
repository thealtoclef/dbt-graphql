# Deployment Guide

How to deploy dbt-graphql in development and production environments.

---

## Quick start

### 1. Install

```bash
pip install dbt-graphql              # core (generate + serve)
pip install dbt-graphql[postgres]    # + asyncpg driver
pip install dbt-graphql[mysql]       # + aiomysql driver
pip install dbt-graphql[redis]       # + Redis-backed cache
```

### 2. Configure

Copy the example config and edit:

```bash
cp config.example.yml config.yml
```

Point `dbt.catalog` and `dbt.manifest` at your dbt artifacts:

```yaml
dbt:
  catalog: target/catalog.json
  manifest: target/manifest.json

db:
  type: postgres
  host: localhost
  port: 5432
  dbname: analytics
  user: dbt
  password: "" # prefer DBT_GRAPHQL__DB__PASSWORD env var
```

### 3. Run

**Generate mode** (no database connection needed):

```bash
dbt-graphql --config config.yml --output ./out
# → out/db.graphql, out/lineage.json
```

**Serve mode**:

```bash
dbt-graphql --config config.yml
# GraphQL API at http://localhost:9876/graphql
```

---

## Production checklist

### 1. Disable dev mode

```yaml
dev_mode: false
```

When `dev_mode` is `false` (or absent), dbt-graphql enforces:

- JWT authentication is required (server fails to start if `security.jwt` is not configured)
- GraphQL introspection is disabled
- Policy evaluation is active for every request

### 2. Configure JWT authentication

```yaml
security:
  jwt:
    algorithms: [RS256]
    audience: dbt-graphql
    issuer: https://your-issuer.example/
    jwks_url: https://your-issuer.example/.well-known/jwks.json
    jwks_cache_ttl: 3600
```

Exactly one key source is required: `jwks_url`, `key_url`, `key_env`, or `key_file`.

### 3. Define access policies

```yaml
security:
  policies:
    - name: analyst
      effect: allow
      when: "'analysts' in jwt.groups"
      tables:
        customers:
          column_level:
            include_all: true
            mask:
              email: "CONCAT('***@', SPLIT_PART(email, '@', 2))"
          row_filter:
            org_id: { _eq: { jwt: claims.org_id } }
```

Default-deny: any table not covered by a matching allow rule produces `FORBIDDEN_TABLE`.

### 4. Disable introspection

```yaml
serve:
  graphql_introspection: false
```

This is the default when `dev_mode: false`. Set to `true` only for development tooling.

### 5. Configure cache (multi-replica)

```yaml
cache:
  url: "redis://redis.internal:6379/0"
  ttl: 60
  lock_safety_timeout: 60
```

Without Redis configuration, the default is an in-memory LRU cache. Use Redis when running multiple replicas behind a load balancer.

### 6. Tune connection pool

```yaml
db:
  pool:
    size: 10
    max_overflow: 20
    timeout: 30
    recycle: 300
    retry_after: 30
```

See [Configuration Reference — db.pool](configuration.md) for full details.

### 7. Configure OpenTelemetry

```yaml
monitoring:
  service_name: dbt-graphql
  traces:
    endpoint: http://otel-collector:4318/v1/traces
    protocol: http
  metrics:
    endpoint: http://otel-collector:4318/v1/metrics
    protocol: http
  logs:
    level: INFO
    endpoint: http://otel-collector:4318/v1/logs
    protocol: http
```

---

## Environment variable overrides

Any config field can be overridden with a `DBT_GRAPHQL__*` environment variable. Nested fields use `__` as delimiter.

| Config path             | Environment variable                   |
| ----------------------- | -------------------------------------- |
| `dbt.catalog`           | `DBT_GRAPHQL__DBT__CATALOG`            |
| `db.host`               | `DBT_GRAPHQL__DB__HOST`                |
| `db.password`           | `DBT_GRAPHQL__DB__PASSWORD`            |
| `serve.port`            | `DBT_GRAPHQL__SERVE__PORT`             |
| `security.jwt.jwks_url` | `DBT_GRAPHQL__SECURITY__JWT__JWKS_URL` |
| `cache.ttl`             | `DBT_GRAPHQL__CACHE__TTL`              |
| `dev_mode`              | `DBT_GRAPHQL__DEV_MODE`                |

**Precedence** (highest to lowest): init arguments > environment variables > config file > defaults.

---

## Docker deployment

### Dockerfile example

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev --extra postgres --extra redis

COPY src/ src/
COPY config.example.yml config.yml

EXPOSE 9876

CMD ["dbt-graphql", "--config", "config.yml"]
```

### Docker Compose example

```yaml
version: "3.8"
services:
  dbt-graphql:
    build: .
    ports:
      - "9876:9876"
    environment:
      DBT_GRAPHQL__DEV_MODE: "false"
      DBT_GRAPHQL__DB__HOST: postgres
      DBT_GRAPHQL__DB__PASSWORD: ${DB_PASSWORD}
      DBT_GRAPHQL__SECURITY__JWT__JWKS_URL: ${JWKS_URL}
      DBT_GRAPHQL__CACHE__URL: "redis://redis:6379/0"
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: analytics
      POSTGRES_USER: dbt
      POSTGRES_PASSWORD: ${DB_PASSWORD}

  redis:
    image: redis:7-alpine
```

---

## Health checks

dbt-graphql exposes OpenTelemetry metrics. Configure your collector to scrape the metrics endpoint and set up alerts on:

- `db.client.connections.wait_time` — connection pool wait times (alert on high p99)
- `cache.result` — cache hit/miss ratio (alert on low hit rate under load)
- `auth.jwt` — JWT verification failures (alert on spike)
- `graphql.request.duration` — request latency (alert on high p99)

If using uvicorn's `--reload` in development, the process auto-restarts on file changes. In production, use a process manager (systemd, supervisord) or container orchestrator.

---

## Reverse proxy setup

### TLS termination (nginx example)

```nginx
server {
    listen 443 ssl;
    server_name graphql.example.com;

    ssl_certificate     /etc/ssl/certs/graphql.pem;
    ssl_certificate_key /etc/ssl/private/graphql-key.pem;

    location /graphql {
        proxy_pass http://127.0.0.1:9876/graphql;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /mcp {
        proxy_pass http://127.0.0.1:9876/mcp;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Notes

- JWT `Authorization: Bearer` headers must pass through the proxy.
- The `Retry-After` header from pool timeout (503) responses is useful for client-side backoff.
- If using a CDN or API gateway in front of dbt-graphql, ensure it does not cache POST responses to `/graphql`.
