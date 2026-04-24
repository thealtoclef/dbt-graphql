from __future__ import annotations

import importlib.resources
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_defaults() -> dict:
    ref = importlib.resources.files("dbt_graphql").joinpath("config.default.yml")
    return yaml.safe_load(ref.read_text())


_DEFAULTS = _load_defaults()


class DbConfig(BaseModel):
    type: str
    host: str = ""
    port: int | None = None
    dbname: str = ""
    user: str = ""
    password: str = ""


class ServeConfig(BaseModel):
    host: str
    port: int


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
    level: str = _DEFAULTS["monitoring"]["logs"]["level"]

    @model_validator(mode="after")
    def _require_protocol_with_endpoint(self) -> "LogsConfig":
        if self.endpoint and not self.protocol:
            raise ValueError(
                "monitoring.logs.protocol is required when endpoint is set"
            )
        return self


class MonitoringConfig(BaseModel):
    service_name: str = _DEFAULTS["monitoring"]["service_name"]
    traces: TracesConfig = TracesConfig()
    metrics: MetricsConfig = MetricsConfig()
    logs: LogsConfig = LogsConfig()


class EnrichmentConfig(BaseModel):
    budget: int = _DEFAULTS["enrichment"]["budget"]
    distinct_values_limit: int = _DEFAULTS["enrichment"]["distinct_values_limit"]
    distinct_values_max_cardinality: int = _DEFAULTS["enrichment"][
        "distinct_values_max_cardinality"
    ]


class SecurityConfig(BaseModel):
    policy_path: Path | None = None


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DBT_GRAPHQL__",
        env_nested_delimiter="__",
    )

    db: DbConfig
    serve: ServeConfig | None = None
    monitoring: MonitoringConfig = MonitoringConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    security: SecurityConfig = SecurityConfig()

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
    """
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("config.yml must be a YAML mapping")
    return AppConfig(**data)
