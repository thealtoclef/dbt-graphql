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

# Cache — three layers (parsed-doc, compiled-plan, result+singleflight).
# All knobs the operator might want to touch live here. Per-layer rationale:
# §5.5 of docs/plans/sec-j-caching.md.
CACHE_BACKEND_DEFAULT_URL: Final[str] = "mem://?size=10000"
CACHE_PARSED_DOC_MAX_SIZE: Final[int] = 1000
CACHE_COMPILED_PLAN_MAX_SIZE: Final[int] = 1000
CACHE_RESULT_DEFAULT_TTL_S: Final[int] = 60
CACHE_RESULT_LOCK_SAFETY_TIMEOUT_S: Final[int] = 60
