"""Build connection-info dicts from dbt profile configuration.

Normalizes adapter-specific connection parameters (host, port, user, etc.)
into flat dicts consumed by Wren output formatter.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..dbt.models import DbtConnection
from .formatter import _DB_TYPE_MAP


# ---------------------------------------------------------------------------
# connection_info builders
# ---------------------------------------------------------------------------


def _build_postgres_info(conn: DbtConnection) -> dict[str, Any]:
    extra = conn.model_extra or {}
    db = extra.get("dbname") or extra.get("database") or ""
    port = int(extra["port"]) if "port" in extra else 5432
    return {
        "host": extra.get("host") or "",
        "port": port,
        "database": db,
        "user": extra.get("user") or "",
        "password": extra.get("password"),
    }


def _build_mssql_info(conn: DbtConnection) -> dict[str, Any]:
    extra = conn.model_extra or {}
    port = int(extra["port"]) if "port" in extra else 1433
    return {
        "host": extra.get("server") or extra.get("host") or "",
        "port": port,
        "database": extra.get("database") or "",
        "user": extra.get("user") or "",
        "password": extra.get("password"),
        "driver": "ODBC Driver 18 for SQL Server",
        "TDS_Version": "8.0",
        "kwargs": {"TrustServerCertificate": "YES"},
    }


def _build_mysql_info(conn: DbtConnection) -> dict[str, Any]:
    extra = conn.model_extra or {}
    port = int(extra["port"]) if "port" in extra else 3306
    ssl_mode = "DISABLED" if extra.get("ssl_disable") else "ENABLED"
    return {
        "host": extra.get("host") or "",
        "port": port,
        "database": extra.get("database") or "",
        "user": extra.get("user") or "",
        "password": extra.get("password"),
        "sslMode": ssl_mode,
    }


def _build_duckdb_info(conn: DbtConnection, dbt_home: Path) -> dict[str, Any]:
    extra = conn.model_extra or {}
    raw_path = extra.get("path") or extra.get("file") or ""
    if not raw_path:
        raise ValueError("duckdb connection missing 'path' field")
    p = Path(raw_path)
    if p.is_absolute():
        url = str(p.parent)
    else:
        url = str((dbt_home / p).parent)
    return {"url": url, "format": "duckdb"}


def _encode_json_bytes(data: bytes) -> str:
    """Validate JSON and return base64-encoded string."""
    try:
        json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Service account JSON is invalid: {exc}") from exc
    return base64.b64encode(data).decode()


def _build_bigquery_info(conn: DbtConnection, dbt_home: Path) -> dict[str, Any]:
    extra = conn.model_extra or {}
    method = (extra.get("method") or "").strip().lower()

    credentials = ""
    if method == "service-account-json":
        raw = extra.get("keyfile_json") or ""
        if not raw:
            raise ValueError(
                "bigquery: method 'service-account-json' requires 'keyfile_json'"
            )
        credentials = _encode_json_bytes(raw.encode())

    elif method in ("service-account", ""):
        keyfile_path = (extra.get("keyfile") or "").strip()
        if not keyfile_path:
            raw = extra.get("keyfile_json") or ""
            if raw:
                credentials = _encode_json_bytes(raw.encode())
            else:
                raise ValueError(
                    "bigquery: method 'service-account' requires 'keyfile' path or "
                    "'keyfile_json'"
                )
        else:
            p = Path(keyfile_path)
            if not p.is_absolute():
                p = dbt_home / p
            p = p.resolve()
            data = p.read_bytes()
            credentials = _encode_json_bytes(data)

    elif method == "oauth":
        raise ValueError("bigquery: oauth auth is not supported")
    else:
        raise ValueError(f"bigquery: unsupported auth method {method!r}")

    return {
        "credentials": credentials,
        "project_id": extra.get("project") or "",
        "dataset_id": extra.get("dataset") or "",
        "bigquery_type": "dataset",
    }


def _build_snowflake_info(conn: DbtConnection) -> dict[str, Any]:
    extra = conn.model_extra or {}
    return {
        "user": extra.get("user") or "",
        "password": extra.get("password"),
        "account": extra.get("account") or "",
        "database": extra.get("database") or "",
        "schema": extra.get("schema") or "",
        "warehouse": extra.get("warehouse"),
    }


def _build_sqlite_info(conn: DbtConnection, dbt_home: Path) -> dict[str, Any]:
    extra = conn.model_extra or {}
    schemas_and_paths: dict = extra.get("schemas_and_paths") or {}
    schema_dir = extra.get("schema_directory") or ""
    schema = extra.get("schema") or "main"

    db_path = schemas_and_paths.get(schema, "")
    if db_path:
        p = Path(db_path)
        if not p.is_absolute():
            p = dbt_home / p
        path = str(p)
    elif schema_dir:
        sd = Path(schema_dir)
        if not sd.is_absolute():
            sd = dbt_home / sd
        path = str(sd / f"{schema}.db")
    else:
        path = ""

    return {
        "path": path,
        "database": extra.get("database") or "database",
        "schema": schema,
    }


def build_connection_info(
    conn: DbtConnection,
    dbt_home: Path,
) -> dict[str, Any]:
    """Build a connection-info dict appropriate for *conn*'s database type."""
    dbt_type = _DB_TYPE_MAP.get(conn.type.strip().lower(), conn.type.strip().lower())

    if dbt_type == "postgres":
        return _build_postgres_info(conn)
    elif dbt_type == "duckdb":
        return _build_duckdb_info(conn, dbt_home)
    elif dbt_type == "mssql":
        return _build_mssql_info(conn)
    elif dbt_type == "mysql":
        return _build_mysql_info(conn)
    elif dbt_type == "bigquery":
        return _build_bigquery_info(conn, dbt_home)
    elif dbt_type == "snowflake":
        return _build_snowflake_info(conn)
    elif dbt_type == "sqlite":
        return _build_sqlite_info(conn, dbt_home)
    else:
        raise ValueError(f"Unsupported dbt adapter type: {conn.type!r}")
