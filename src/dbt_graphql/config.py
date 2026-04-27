from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import defaults
from .cache.config import CacheConfig


class PoolConfig(BaseModel):
    """SQLAlchemy connection-pool tuning.

    The pool is the admission queue: requests beyond ``size + max_overflow``
    block on checkout, and are fast-failed with ``TimeoutError`` after
    ``timeout`` seconds. Set ``timeout`` below your upstream LB idle timeout
    so the API returns 503+Retry-After before the LB resets the connection.
    """

    size: int = defaults.DB_POOL_SIZE
    max_overflow: int = defaults.DB_POOL_MAX_OVERFLOW
    timeout: int = defaults.DB_POOL_TIMEOUT
    recycle: int = defaults.DB_POOL_RECYCLE
    # Emitted as ``Retry-After: <value>`` on 503 responses (seconds, per
    # RFC 9110 §10.2.3). See ``DB_POOL_RETRY_AFTER`` in defaults.py.
    retry_after: int = defaults.DB_POOL_RETRY_AFTER


class DbConfig(BaseModel):
    type: str
    host: str = ""
    port: int | None = None
    dbname: str = ""
    user: str = ""
    password: str = ""
    pool: PoolConfig = PoolConfig()


class ServeConfig(BaseModel):
    host: str
    port: int
    graphql: bool = False
    mcp: bool = False


class TracesConfig(BaseModel):
    endpoint: str | None = None
    protocol: str | None = None  # "grpc" or "http"; required when endpoint is set

    @model_validator(mode="after")
    def _require_protocol_with_endpoint(self) -> "TracesConfig":
        if self.endpoint and not self.protocol:
            raise ValueError(
                "monitoring.traces.protocol is required when endpoint is set"
            )
        return self


class MetricsConfig(BaseModel):
    endpoint: str | None = None
    protocol: str | None = None

    @model_validator(mode="after")
    def _require_protocol_with_endpoint(self) -> "MetricsConfig":
        if self.endpoint and not self.protocol:
            raise ValueError(
                "monitoring.metrics.protocol is required when endpoint is set"
            )
        return self


class LogsConfig(BaseModel):
    endpoint: str | None = None
    protocol: str | None = None
    level: str = defaults.MONITORING_LOG_LEVEL

    @model_validator(mode="after")
    def _require_protocol_with_endpoint(self) -> "LogsConfig":
        if self.endpoint and not self.protocol:
            raise ValueError(
                "monitoring.logs.protocol is required when endpoint is set"
            )
        return self


class MonitoringConfig(BaseModel):
    service_name: str = defaults.MONITORING_SERVICE_NAME
    traces: TracesConfig = TracesConfig()
    metrics: MetricsConfig = MetricsConfig()
    logs: LogsConfig = LogsConfig()


class EnrichmentConfig(BaseModel):
    budget: int = defaults.ENRICHMENT_BUDGET
    distinct_values_limit: int = defaults.ENRICHMENT_DISTINCT_VALUES_LIMIT
    distinct_values_max_cardinality: int = (
        defaults.ENRICHMENT_DISTINCT_VALUES_MAX_CARDINALITY
    )


class JWTConfig(BaseModel):
    enabled: bool = False
    algorithms: list[str] = []
    audience: str | list[str] | None = None
    issuer: str | None = None
    leeway: int = defaults.JWT_LEEWAY
    required_claims: list[str] = ["exp"]
    roles_claim: str = "scope"

    jwks_url: HttpUrl | None = None
    jwks_cache_ttl: int = defaults.JWT_JWKS_CACHE_TTL
    key_url: HttpUrl | None = None
    key_env: str | None = None
    key_file: Path | None = None

    @model_validator(mode="after")
    def _validate(self) -> "JWTConfig":
        if not self.enabled:
            return self
        if not self.algorithms:
            raise ValueError("security.jwt.algorithms is required when enabled")
        sources = [self.jwks_url, self.key_url, self.key_env, self.key_file]
        if sum(s is not None for s in sources) != 1:
            raise ValueError(
                "security.jwt requires exactly one of: "
                "jwks_url, key_url, key_env, key_file"
            )
        return self


class SecurityConfig(BaseModel):
    policy_path: Path | None = None
    jwt: JWTConfig = JWTConfig()


class DbtConfig(BaseModel):
    catalog: Path
    manifest: Path
    exclude: list[str] = []


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DBT_GRAPHQL__",
        env_nested_delimiter="__",
    )

    dbt: DbtConfig
    db: DbConfig | None = None
    serve: ServeConfig | None = None
    monitoring: MonitoringConfig = MonitoringConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    security: SecurityConfig = SecurityConfig()
    cache: CacheConfig = CacheConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,  # noqa: ARG003 — required for override signature
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # env vars take precedence over config file (init_settings)
        return env_settings, init_settings, dotenv_settings, file_secret_settings


def load_config(path: str | Path) -> AppConfig:
    """Load config.yml and merge with DBT_GRAPHQL__* environment variables.

    Env vars override file values. Example: DBT_GRAPHQL__ENRICHMENT__BUDGET=5
    Relative paths for catalog and manifest are resolved against the config file's directory.
    """
    config_path = Path(path).resolve()
    config_dir = config_path.parent
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        raise ValueError("config.yml must be a YAML mapping")
    dbt = data.get("dbt", {})
    if isinstance(dbt, dict):
        for field in ("catalog", "manifest"):
            if field in dbt and dbt[field]:
                p = Path(str(dbt[field]))
                if not p.is_absolute():
                    dbt[field] = str(config_dir / p)
        data["dbt"] = dbt
    return AppConfig(**data)
