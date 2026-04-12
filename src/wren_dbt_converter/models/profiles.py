from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class DbtConnection(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    host: Optional[str] = None
    server: Optional[str] = None  # MSSQL
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    dbname: Optional[str] = None  # Postgres alternative
    database: Optional[str] = None
    schema_: Optional[str] = None  # stored as "schema" in YAML; avoid shadowing
    # BigQuery
    project: Optional[str] = None
    dataset: Optional[str] = None
    keyfile: Optional[str] = None
    method: Optional[str] = None
    # Snowflake
    account: Optional[str] = None
    warehouse: Optional[str] = None
    role: Optional[str] = None
    # Postgres extras
    keepalive: Optional[bool] = None
    search_path: Optional[str] = None
    sslmode: Optional[str] = None
    # MySQL
    ssl_disable: Optional[bool] = None
    # DuckDB
    path: Optional[str] = None

    def get_extra(self, key: str) -> Any:
        """Return an extra field not in the standard set (e.g. keyfile_json)."""
        return self.model_extra.get(key) if self.model_extra else None


class DbtProfile(BaseModel):
    target: str
    outputs: dict[str, DbtConnection]


class DbtProfiles(BaseModel):
    profiles: dict[str, DbtProfile]
