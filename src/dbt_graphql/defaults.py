"""Hard-coded default values for AppConfig fields.

Single source of truth — referenced by `config.py` and `config.example.yml`
documentation. Keep these in sync with `docs/configuration.md`.
"""

from __future__ import annotations

from typing import Final


# Enrichment — live DB queries issued by `describe_table` in the MCP server.
ENRICHMENT_BUDGET: Final[int] = 20
ENRICHMENT_DISTINCT_VALUES_LIMIT: Final[int] = 50
ENRICHMENT_DISTINCT_VALUES_MAX_CARDINALITY: Final[int] = 500

# Monitoring — OTel resource attributes and log level.
MONITORING_SERVICE_NAME: Final[str] = "dbt-graphql"
MONITORING_LOG_LEVEL: Final[str] = "INFO"

# Cache — result cache + singleflight. All knobs the operator might want
# to touch live here.
CACHE_DEFAULT_URL: Final[str] = "mem://?size=10000"
CACHE_TTL: Final[int] = 60
CACHE_LOCK_SAFETY_TIMEOUT: Final[int] = 10

# DB connection pool. Set ``pool_timeout`` below your upstream LB idle timeout
# so the API can fast-fail with 503+Retry-After before the LB starts killing
# connections. Defaults target a single replica behind a 30s LB; tune per
# warehouse capacity and replica count.
DB_POOL_SIZE: Final[int] = 20
DB_POOL_MAX_OVERFLOW: Final[int] = 10
DB_POOL_TIMEOUT: Final[int] = 10
DB_POOL_RECYCLE: Final[int] = 1800
# Hint emitted in the ``Retry-After`` header when a 503 is returned for pool
# saturation. Should approximate "how long until capacity is likely free"
# (~p50 warehouse query time), NOT the pool wait timeout.
DB_POOL_RETRY_AFTER: Final[int] = 5

# JWT — clock-skew tolerance and JWKS cache TTL.
JWT_LEEWAY: Final[int] = 30
JWT_JWKS_CACHE_TTL: Final[int] = 3600

# Query guards — pre-execution limits on incoming GraphQL queries.
QUERY_MAX_DEPTH: Final[int] = 5
QUERY_MAX_FIELDS: Final[int] = 50
# Caps integer literals on ``limit:`` / ``first:`` resolver arguments so a
# trivial query can't ask for an unbounded warehouse scan. Variables bypass
# this rule by design (validation runs before binding); resolvers must
# apply runtime caps when accepting variables for pagination.
QUERY_MAX_LIST_LIMIT: Final[int] = 1000
