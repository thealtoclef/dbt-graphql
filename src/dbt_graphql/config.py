from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .graphql.policy import PolicyEntry

# Default values — single source of truth for AppConfig fields.
# Keep in sync with docs/configuration.md.
DEFAULT = {
    # Monitoring — OTel resource attributes and log level.
    "MONITORING_SERVICE_NAME": "dbt-graphql",
    "MONITORING_LOG_LEVEL": "INFO",
    # Cache — result cache + singleflight.
    "CACHE_DEFAULT_URL": "mem://?size=10000",
    "CACHE_TTL": 60,
    "CACHE_LOCK_SAFETY_TIMEOUT": 10,
    # DB connection pool.
    "DB_POOL_SIZE": 20,
    "DB_POOL_MAX_OVERFLOW": 10,
    "DB_POOL_TIMEOUT": 10,
    "DB_POOL_RECYCLE": 1800,
    "DB_POOL_RETRY_AFTER": 5,
    # JWT.
    "JWT_LEEWAY": 30,
    "JWT_JWKS_CACHE_TTL": 3600,
    # Query guards.
    "QUERY_MAX_DEPTH": 5,
    "QUERY_MAX_FIELDS": 50,
    "QUERY_MAX_LIMIT": 1000,
}


class CacheConfig(BaseModel):
    """Cache configuration for result cache + singleflight."""

    url: str = DEFAULT["CACHE_DEFAULT_URL"]
    ttl: int = DEFAULT["CACHE_TTL"]
    lock_safety_timeout: int = DEFAULT["CACHE_LOCK_SAFETY_TIMEOUT"]


class PoolConfig(BaseModel):
    """SQLAlchemy connection-pool tuning.

    The pool is the admission queue: requests beyond ``size + max_overflow``
    block on checkout, and are fast-failed with ``TimeoutError`` after
    ``timeout`` seconds. Set ``timeout`` below your upstream LB idle timeout
    so the API returns 503+Retry-After before the LB resets the connection.
    """

    size: int = DEFAULT["DB_POOL_SIZE"]
    max_overflow: int = DEFAULT["DB_POOL_MAX_OVERFLOW"]
    timeout: float = DEFAULT["DB_POOL_TIMEOUT"]
    recycle: int = DEFAULT["DB_POOL_RECYCLE"]
    # Emitted as ``Retry-After: <value>`` on 503 responses (seconds, per
    # RFC 9110 §10.2.3). See ``DEFAULT["DB_POOL_RETRY_AFTER"]``.
    retry_after: int = DEFAULT["DB_POOL_RETRY_AFTER"]


class DbConfig(BaseModel):
    type: str
    host: str = ""
    port: int | None = None
    dbname: str = ""
    user: str = ""
    password: str = ""
    pool: PoolConfig = PoolConfig()


class ServeConfig(BaseModel):
    """HTTP serve config. GraphQL is always mounted at ``/graphql``; MCP
    is opt-in via ``mcp_enabled`` and mounts at ``/mcp`` when on.
    """

    host: str
    port: int
    mcp_enabled: bool = False


class _OTLPSignalConfig(BaseModel):
    """Shared OTLP exporter shape — endpoint requires protocol when set."""

    endpoint: str | None = None
    protocol: str | None = None  # "grpc" or "http"; required when endpoint is set

    @model_validator(mode="after")
    def _require_protocol_with_endpoint(self):
        if self.endpoint and not self.protocol:
            signal = type(self).__name__.removesuffix("Config").lower()
            raise ValueError(
                f"monitoring.{signal}.protocol is required when endpoint is set"
            )
        return self


class TracesConfig(_OTLPSignalConfig):
    pass


class MetricsConfig(_OTLPSignalConfig):
    pass


class LogsConfig(_OTLPSignalConfig):
    level: str = DEFAULT["MONITORING_LOG_LEVEL"]


class MonitoringConfig(BaseModel):
    service_name: str = DEFAULT["MONITORING_SERVICE_NAME"]
    traces: TracesConfig = TracesConfig()
    metrics: MetricsConfig = MetricsConfig()
    logs: LogsConfig = LogsConfig()


class GraphQLConfig(BaseModel):
    """Query guard limits applied to all incoming GraphQL operations."""

    query_max_depth: int = DEFAULT["QUERY_MAX_DEPTH"]
    query_max_fields: int = DEFAULT["QUERY_MAX_FIELDS"]
    # ``None`` disables the list-limit cap entirely.
    query_max_limit: int | None = DEFAULT["QUERY_MAX_LIMIT"]


class JWTConfig(BaseModel):
    """JWT verification settings. Ignored when the app is started in
    ``dev_mode`` — every request is then treated as anonymous.
    """

    algorithms: list[str] = []
    audience: str | list[str] | None = None
    issuer: str | None = None
    leeway: int = DEFAULT["JWT_LEEWAY"]
    required_claims: list[str] = ["exp"]
    roles_claim: str = "scope"

    jwks_url: HttpUrl | None = None
    jwks_cache_ttl: int = DEFAULT["JWT_JWKS_CACHE_TTL"]
    key_url: HttpUrl | None = None
    key_env: str | None = None
    key_file: Path | None = None

    @model_validator(mode="after")
    def _validate(self) -> JWTConfig:
        # Reject obviously-mistyped key_url's (JWKS endpoints) regardless of
        # whether security is enabled — a typo here always indicates a bug.
        if self.key_url is not None:
            url_str = str(self.key_url).lower()
            if "jwks" in url_str or url_str.endswith("/.well-known/jwks.json"):
                raise ValueError(
                    "security.jwt.key_url looks like a JWKS endpoint "
                    f"({self.key_url!s}). Use jwks_url for rotating key sets — "
                    "key_url is for a single static PEM/JWK and is fetched "
                    "once with no refresh."
                )
        return self


class SecurityConfig(BaseModel):
    """Authn (JWT) and authz (access policies). Both are enforced unless
    the app is started with ``dev_mode: true`` at the root of the config.
    """

    jwt: JWTConfig = JWTConfig()
    # Access policies declared inline under ``security.policies`` —
    # centralized config, no separate access.yml roundtrip. Empty list means
    # authn-only (no row/column enforcement).
    policies: list[PolicyEntry] = []


class DbtConfig(BaseModel):
    # fsspec URIs: bare paths and ``file://`` resolve locally; remote schemes
    # (``gs://``, ``s3://``, ``http(s)://``, ...) require the matching extra
    # to be installed (``pip install dbt-graphql[gcs]`` / ``[s3]``).
    catalog: str
    manifest: str
    exclude: list[str] = []


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DBT_GRAPHQL__",
        env_nested_delimiter="__",
    )

    # ``dev_mode`` is the single profile switch. When true: authn/authz are
    # bypassed (every request is anonymous, no policy evaluation). Default
    # false → secure-by-default; forgetting to configure ``security.jwt``
    # then fails at startup, not silently. (Standard ``__schema``
    # introspection is always on regardless of dev_mode — the auth-aware
    # view is the policy-pruned ``Query._sdl`` field.)
    dev_mode: bool = False
    dbt: DbtConfig
    db: DbConfig | None = None
    serve: ServeConfig | None = None
    monitoring: MonitoringConfig = MonitoringConfig()
    graphql: GraphQLConfig = GraphQLConfig()
    security: SecurityConfig = SecurityConfig()
    cache: CacheConfig = CacheConfig()

    @model_validator(mode="after")
    def _validate_security(self) -> AppConfig:
        # Security only matters when actually serving traffic. Generate-mode
        # invocations (``--output``) load the config without ``serve:`` set
        # and shouldn't be forced to declare a JWT block.
        if self.dev_mode or self.serve is None:
            return self
        jwt = self.security.jwt
        if not jwt.algorithms:
            raise ValueError(
                "security.jwt.algorithms is required when dev_mode is false"
            )
        sources = [jwt.jwks_url, jwt.key_url, jwt.key_env, jwt.key_file]
        if sum(s is not None for s in sources) != 1:
            raise ValueError(
                "security.jwt requires exactly one of: "
                "jwks_url, key_url, key_env, key_file"
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        del settings_cls  # required override param; unused
        # env vars take precedence over config file (init_settings)
        return env_settings, init_settings, dotenv_settings, file_secret_settings


def load_config(path: str | Path | None = None) -> AppConfig:
    """Build :class:`AppConfig` from an optional YAML file plus env vars.

    ``DBT_GRAPHQL__*`` environment variables are always read and take
    precedence over file values (see ``settings_customise_sources``). When
    ``path`` is ``None``, the file source is skipped and configuration is
    sourced from env vars alone. Example:
    ``DBT_GRAPHQL__DBT__CATALOG=gs://bkt/catalog.json``.

    ``dbt.catalog`` and ``dbt.manifest`` are passed verbatim to fsspec, so
    any supported URI works (``gs://``, ``s3://``, ``http(s)://``,
    ``file://``, or a bare path interpreted as local).
    """
    if path is None:
        return AppConfig()  # type: ignore[ty:missing-argument]
    config_path = Path(path).resolve()
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        raise ValueError("config.yml must be a YAML mapping")
    return AppConfig(**data)
