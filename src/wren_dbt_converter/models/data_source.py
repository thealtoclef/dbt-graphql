from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from wren import DataSource as WrenDataSource

from .profiles import DbtConnection, DbtProfiles

# ---------------------------------------------------------------------------
# dbt type → wren DataSource
# ---------------------------------------------------------------------------

_DBT_TYPE_MAP: dict[str, WrenDataSource] = {
    "postgres": WrenDataSource.postgres,
    "postgresql": WrenDataSource.postgres,
    "duckdb": WrenDataSource.duckdb,
    "sqlserver": WrenDataSource.mssql,
    "mysql": WrenDataSource.mysql,
    "bigquery": WrenDataSource.bigquery,
    "snowflake": WrenDataSource.snowflake,
}


def map_dbt_type_to_wren(dbt_type: str) -> WrenDataSource:
    key = dbt_type.strip().lower()
    ds = _DBT_TYPE_MAP.get(key)
    if ds is None:
        raise ValueError(f"Unsupported dbt adapter type: {dbt_type!r}")
    return ds


# ---------------------------------------------------------------------------
# connection_info builders — return plain dicts matching wren-engine models
# ---------------------------------------------------------------------------


def _build_postgres_info(conn: DbtConnection) -> dict[str, Any]:
    db = conn.dbname or conn.database or ""
    port = str(conn.port) if conn.port else "5432"
    return {
        "host": conn.host or "",
        "port": port,
        "database": db,
        "user": conn.user or "",
        "password": conn.password,
    }


def _build_mssql_info(conn: DbtConnection) -> dict[str, Any]:
    port = str(conn.port) if conn.port else "1433"
    return {
        "host": conn.server or conn.host or "",
        "port": port,
        "database": conn.database or "",
        "user": conn.user or "",
        "password": conn.password,
        "driver": "ODBC Driver 18 for SQL Server",
        "TDS_Version": "8.0",
        "kwargs": {"TrustServerCertificate": "YES"},
    }


def _build_mysql_info(conn: DbtConnection) -> dict[str, Any]:
    port = str(conn.port) if conn.port else "3306"
    ssl_mode = "DISABLED" if conn.ssl_disable else "ENABLED"
    return {
        "host": conn.host or "",
        "port": port,
        "database": conn.database or "",
        "user": conn.user or "",
        "password": conn.password,
        "sslMode": ssl_mode,
    }


def _build_duckdb_info(conn: DbtConnection, dbt_home: Path) -> dict[str, Any]:
    raw_path = conn.path or conn.get_extra("file") or ""
    if not raw_path:
        raise ValueError("duckdb connection missing 'path' field")
    p = Path(raw_path)
    if p.is_absolute():
        url = str(p.parent)
    else:
        base = dbt_home if dbt_home else Path.cwd()
        url = str((base / p).parent)
    return {"url": url, "format": "duckdb"}


def _encode_json_bytes(data: bytes) -> str:
    """Validate JSON and return base64-encoded string."""
    try:
        json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Service account JSON is invalid: {exc}") from exc
    return base64.b64encode(data).decode()


def _build_bigquery_info(conn: DbtConnection, dbt_home: Path) -> dict[str, Any]:
    method = (conn.method or "").strip().lower()

    credentials: str
    if method == "service-account-json":
        raw = conn.get_extra("keyfile_json") or ""
        if not raw:
            raise ValueError(
                "bigquery: method 'service-account-json' requires 'keyfile_json'"
            )
        credentials = _encode_json_bytes(raw.encode())

    elif method in ("service-account", ""):
        keyfile_path = (conn.keyfile or conn.get_extra("keyfile") or "").strip()
        if not keyfile_path:
            # Fallback: try inline keyfile_json even for service-account method
            raw = conn.get_extra("keyfile_json") or ""
            if raw:
                credentials = _encode_json_bytes(raw.encode())
            else:
                raise ValueError(
                    "bigquery: method 'service-account' requires 'keyfile' path"
                )
        else:
            p = Path(keyfile_path)
            if not p.is_absolute() and dbt_home:
                p = dbt_home / p
            p = p.resolve()
            try:
                data = p.read_bytes()
            except OSError as exc:
                raise ValueError(f"Failed to read keyfile '{p}': {exc}") from exc
            credentials = _encode_json_bytes(data)

    elif method == "oauth":
        raise ValueError("bigquery: oauth auth method is not supported")
    else:
        raise ValueError(
            f"bigquery: unsupported auth method {method!r}; "
            "supported: service-account, service-account-json"
        )

    return {
        "credentials": credentials,
        "project_id": conn.project or "",
        "dataset_id": conn.dataset or "",
        "bigquery_type": "dataset",
    }


def _build_snowflake_info(conn: DbtConnection) -> dict[str, Any]:
    return {
        "user": conn.user or "",
        "password": conn.password,
        "account": conn.account or "",
        "database": conn.database or "",
        "schema": conn.schema_ or "",
        "warehouse": conn.warehouse,
    }


def build_connection_info(
    conn: DbtConnection,
    dbt_home: Path | None = None,
) -> dict[str, Any]:
    dbt_home = dbt_home or Path.cwd()
    dbt_type = conn.type.strip().lower()

    if dbt_type in ("postgres", "postgresql"):
        return _build_postgres_info(conn)
    elif dbt_type == "duckdb":
        return _build_duckdb_info(conn, dbt_home)
    elif dbt_type == "sqlserver":
        return _build_mssql_info(conn)
    elif dbt_type == "mysql":
        return _build_mysql_info(conn)
    elif dbt_type == "bigquery":
        return _build_bigquery_info(conn, dbt_home)
    elif dbt_type == "snowflake":
        return _build_snowflake_info(conn)
    else:
        raise ValueError(f"Unsupported dbt adapter type: {conn.type!r}")


def get_active_connection(
    profiles: DbtProfiles,
    profile_name: str | None,
    target: str | None,
    dbt_home: Path | None = None,
) -> tuple[WrenDataSource, dict[str, Any]]:
    """Return (WrenDataSource, connection_info_dict) for the active target."""
    if not profiles.profiles:
        raise ValueError("profiles is empty")

    name = profile_name or next(iter(profiles.profiles))
    profile = profiles.profiles.get(name)
    if profile is None:
        raise KeyError(f"Profile {name!r} not found")

    tgt = target or profile.target
    conn = profile.outputs.get(tgt)
    if conn is None:
        raise KeyError(f"Target {tgt!r} not found in profile {name!r}")

    data_source = map_dbt_type_to_wren(conn.type)
    connection_info = build_connection_info(conn, dbt_home)
    return data_source, connection_info


# ---------------------------------------------------------------------------
# Column type mapping
# ---------------------------------------------------------------------------


def map_column_type(data_source: WrenDataSource, source_type: str) -> str:
    """Map a database-native column type to a Wren MDL type string."""
    upper = source_type.upper().strip()

    if data_source == WrenDataSource.bigquery:
        return _map_bigquery_type(upper)
    elif data_source == WrenDataSource.duckdb:
        return _map_duckdb_type(upper)
    elif data_source == WrenDataSource.mssql:
        return _map_mssql_type(source_type.lower().strip())
    elif data_source == WrenDataSource.mysql:
        return _map_mysql_type(upper)
    else:
        # postgres, snowflake, redshift — pass through lowercase
        return source_type.lower()


def _map_bigquery_type(upper: str) -> str:
    match upper:
        case "INT64" | "INTEGER" | "INT":
            return "integer"
        case "FLOAT64" | "FLOAT":
            return "double"
        case "STRING":
            return "varchar"
        case "BOOL" | "BOOLEAN":
            return "boolean"
        case "DATE":
            return "date"
        case "TIMESTAMP" | "DATETIME":
            return "timestamp"
        case "NUMERIC" | "DECIMAL" | "BIGNUMERIC":
            return "double"
        case "BYTES" | "JSON":
            return "varchar"
        case _:
            return upper.lower()


def _map_duckdb_type(upper: str) -> str:
    match upper:
        case "INTEGER" | "INT" | "BIGINT" | "INT64":
            return "integer"
        case "VARCHAR" | "TEXT" | "STRING":
            return "varchar"
        case "DATE":
            return "date"
        case "TIMESTAMP" | "DATETIME":
            return "timestamp"
        case "DOUBLE" | "FLOAT" | "NUMERIC" | "DECIMAL":
            return "double"
        case "BOOLEAN" | "BOOL":
            return "boolean"
        case "JSON":
            return "json"
        case _:
            return upper.lower()


def _map_mssql_type(lower: str) -> str:
    match lower:
        case "char" | "nchar":
            return "char"
        case "varchar" | "nvarchar":
            return "varchar"
        case "text" | "ntext":
            return "text"
        case "bit" | "tinyint":
            return "boolean"
        case "smallint":
            return "smallint"
        case "int":
            return "integer"
        case "bigint":
            return "bigint"
        case "boolean":
            return "boolean"
        case "float" | "real":
            return "float"
        case "decimal" | "numeric" | "money" | "smallmoney":
            return "decimal"
        case "date":
            return "date"
        case "datetime" | "datetime2" | "smalldatetime":
            return "timestamp"
        case "time":
            return "interval"
        case "datetimeoffset":
            return "timestamptz"
        case "json":
            return "json"
        case _:
            return lower


def _map_mysql_type(upper: str) -> str:
    match upper:
        case "CHAR":
            return "char"
        case "VARCHAR":
            return "varchar"
        case "TEXT" | "TINYTEXT" | "MEDIUMTEXT" | "LONGTEXT" | "ENUM" | "SET":
            return "text"
        case "BIT" | "TINYINT":
            return "TINYINT"
        case "SMALLINT":
            return "SMALLINT"
        case "MEDIUMINT" | "INT" | "INTEGER":
            return "INTEGER"
        case "BIGINT":
            return "BIGINT"
        case "FLOAT" | "DOUBLE":
            return "DOUBLE"
        case "DECIMAL" | "NUMERIC":
            return "DECIMAL"
        case "DATE":
            return "DATE"
        case "DATETIME":
            return "DATETIME"
        case "TIMESTAMP":
            return "TIMESTAMPTZ"
        case "BOOLEAN" | "BOOL":
            return "BOOLEAN"
        case "JSON":
            return "JSON"
        case _:
            return upper.lower()
