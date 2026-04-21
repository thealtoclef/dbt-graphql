from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


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


class MonitoringConfig(BaseModel):
    service_name: str = "dbt-graphql"
    exporter: str = "otlp"
    endpoint: str | None = None
    log_level: str = "INFO"


class AppConfig(BaseModel):
    db: DbConfig
    serve: ServeConfig | None = None
    monitoring: MonitoringConfig = MonitoringConfig()


def load_config(path: str | Path) -> AppConfig:
    """Load and validate ``config.yml``."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("config.yml must be a YAML mapping")
    return AppConfig.model_validate(data)
