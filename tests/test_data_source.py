import base64
import json
import pytest

from wren import DataSource as WrenDataSource

from wren_dbt_converter.models.data_source import (
    map_dbt_type_to_wren,
    build_connection_info,
    get_active_connection,
    map_column_type,
)
from wren_dbt_converter.models.profiles import DbtConnection, DbtProfile, DbtProfiles


# ---------------------------------------------------------------------------
# map_dbt_type_to_wren
# ---------------------------------------------------------------------------


def test_map_postgres():
    assert map_dbt_type_to_wren("postgres") == WrenDataSource.postgres
    assert map_dbt_type_to_wren("postgresql") == WrenDataSource.postgres


def test_map_duckdb():
    assert map_dbt_type_to_wren("duckdb") == WrenDataSource.duckdb


def test_map_sqlserver():
    assert map_dbt_type_to_wren("sqlserver") == WrenDataSource.mssql


def test_map_mysql():
    assert map_dbt_type_to_wren("mysql") == WrenDataSource.mysql


def test_map_bigquery():
    assert map_dbt_type_to_wren("bigquery") == WrenDataSource.bigquery


def test_map_snowflake():
    assert map_dbt_type_to_wren("snowflake") == WrenDataSource.snowflake


def test_map_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        map_dbt_type_to_wren("oracle")


# ---------------------------------------------------------------------------
# build_connection_info — postgres
# ---------------------------------------------------------------------------


def test_postgres_connection_info():
    conn = DbtConnection(
        type="postgres",
        host="localhost",
        port=5432,
        database="mydb",
        user="admin",
        password="secret",
    )
    info = build_connection_info(conn)
    assert info["host"] == "localhost"
    assert info["port"] == "5432"
    assert info["database"] == "mydb"
    assert info["user"] == "admin"
    assert info["password"] == "secret"


def test_postgres_uses_dbname_over_database():
    conn = DbtConnection(
        type="postgres",
        host="localhost",
        port=5432,
        dbname="jaffle_shop",
        database="other",
        user="u",
        password="p",
    )
    info = build_connection_info(conn)
    assert info["database"] == "jaffle_shop"


def test_postgres_default_port():
    conn = DbtConnection(type="postgres", host="h", database="db", user="u")
    info = build_connection_info(conn)
    assert info["port"] == "5432"


# ---------------------------------------------------------------------------
# build_connection_info — mssql
# ---------------------------------------------------------------------------


def test_mssql_connection_info():
    conn = DbtConnection(
        type="sqlserver",
        server="sql-host",
        port=1433,
        database="mydb",
        user="sa",
        password="pw",
    )
    info = build_connection_info(conn)
    assert info["host"] == "sql-host"
    assert info["port"] == "1433"
    assert info["database"] == "mydb"
    assert info["TDS_Version"] == "8.0"
    assert info["driver"] == "ODBC Driver 18 for SQL Server"
    assert info["kwargs"]["TrustServerCertificate"] == "YES"


def test_mssql_default_port():
    conn = DbtConnection(type="sqlserver", server="h", database="db", user="u")
    info = build_connection_info(conn)
    assert info["port"] == "1433"


# ---------------------------------------------------------------------------
# build_connection_info — mysql
# ---------------------------------------------------------------------------


def test_mysql_connection_info():
    conn = DbtConnection(
        type="mysql",
        host="mysql-host",
        port=3306,
        database="mydb",
        user="root",
        password="pw",
    )
    info = build_connection_info(conn)
    assert info["host"] == "mysql-host"
    assert info["port"] == "3306"
    assert info["sslMode"] == "ENABLED"


def test_mysql_ssl_disabled():
    conn = DbtConnection(
        type="mysql", host="h", port=3306, database="db", user="u", ssl_disable=True
    )
    info = build_connection_info(conn)
    assert info["sslMode"] == "DISABLED"


# ---------------------------------------------------------------------------
# build_connection_info — duckdb
# ---------------------------------------------------------------------------


def test_duckdb_absolute_path(tmp_path):
    db_file = tmp_path / "data" / "shop.duckdb"
    conn = DbtConnection(type="duckdb", path=str(db_file))
    info = build_connection_info(conn)
    assert info["url"] == str(tmp_path / "data")
    assert info["format"] == "duckdb"


def test_duckdb_relative_path(tmp_path):
    conn = DbtConnection(type="duckdb", path="subdir/shop.duckdb")
    info = build_connection_info(conn, dbt_home=tmp_path)
    assert info["url"] == str(tmp_path / "subdir")


def test_duckdb_missing_path():
    conn = DbtConnection(type="duckdb")
    with pytest.raises(ValueError, match="missing 'path'"):
        build_connection_info(conn)


# ---------------------------------------------------------------------------
# build_connection_info — bigquery
# ---------------------------------------------------------------------------


def _valid_json_b64(data: dict) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


SAMPLE_SA = {"type": "service_account", "project_id": "proj"}


def test_bigquery_service_account_json():
    raw = json.dumps(SAMPLE_SA)
    conn = DbtConnection(
        type="bigquery",
        method="service-account-json",
        project="proj",
        dataset="ds",
        **{"keyfile_json": raw},
    )
    info = build_connection_info(conn)
    assert info["project_id"] == "proj"
    assert info["dataset_id"] == "ds"
    decoded = base64.b64decode(info["credentials"])
    assert json.loads(decoded) == SAMPLE_SA


def test_bigquery_service_account_absolute_keyfile(tmp_path):
    kf = tmp_path / "sa.json"
    kf.write_text(json.dumps(SAMPLE_SA))
    conn = DbtConnection(
        type="bigquery",
        method="service-account",
        project="proj",
        dataset="ds",
        keyfile=str(kf),
    )
    info = build_connection_info(conn)
    decoded = base64.b64decode(info["credentials"])
    assert json.loads(decoded) == SAMPLE_SA


def test_bigquery_service_account_relative_keyfile(tmp_path):
    (tmp_path / "keys").mkdir()
    kf = tmp_path / "keys" / "sa.json"
    kf.write_text(json.dumps(SAMPLE_SA))
    conn = DbtConnection(
        type="bigquery",
        method="service-account",
        project="proj",
        dataset="ds",
        keyfile="keys/sa.json",
    )
    info = build_connection_info(conn, dbt_home=tmp_path)
    decoded = base64.b64decode(info["credentials"])
    assert json.loads(decoded) == SAMPLE_SA


def test_bigquery_oauth_raises():
    conn = DbtConnection(type="bigquery", method="oauth", project="p", dataset="d")
    with pytest.raises(ValueError, match="oauth"):
        build_connection_info(conn)


def test_bigquery_missing_keyfile_raises():
    conn = DbtConnection(
        type="bigquery", method="service-account", project="p", dataset="d"
    )
    with pytest.raises(ValueError, match="requires 'keyfile'"):
        build_connection_info(conn)


def test_bigquery_invalid_json_raises():
    conn = DbtConnection(
        type="bigquery",
        method="service-account-json",
        project="p",
        dataset="d",
        **{"keyfile_json": "not-json"},
    )
    with pytest.raises(ValueError, match="invalid"):
        build_connection_info(conn)


# ---------------------------------------------------------------------------
# get_active_connection
# ---------------------------------------------------------------------------


def _make_profiles(**kwargs) -> DbtProfiles:
    return DbtProfiles(
        profiles={
            "myproject": DbtProfile(
                target="dev",
                outputs={
                    "dev": DbtConnection(
                        type="postgres",
                        host="localhost",
                        port=5432,
                        database="dev_db",
                        user="dev_user",
                    ),
                    "prod": DbtConnection(
                        type="postgres",
                        host="prod-host",
                        port=5432,
                        database="prod_db",
                        user="prod_user",
                    ),
                },
            )
        }
    )


def test_get_active_uses_default_target():
    profiles = _make_profiles()
    ds, info = get_active_connection(profiles, "myproject", None)
    assert ds == WrenDataSource.postgres
    assert info["database"] == "dev_db"


def test_get_active_explicit_target():
    profiles = _make_profiles()
    _, info = get_active_connection(profiles, "myproject", "prod")
    assert info["database"] == "prod_db"


def test_get_active_first_profile_if_none():
    profiles = _make_profiles()
    ds, info = get_active_connection(profiles, None, None)
    assert ds == WrenDataSource.postgres


def test_get_active_missing_profile():
    profiles = _make_profiles()
    with pytest.raises(KeyError, match="nonexistent"):
        get_active_connection(profiles, "nonexistent", None)


def test_get_active_missing_target():
    profiles = _make_profiles()
    with pytest.raises(KeyError, match="staging"):
        get_active_connection(profiles, "myproject", "staging")


# ---------------------------------------------------------------------------
# map_column_type
# ---------------------------------------------------------------------------


def test_bigquery_int64():
    assert map_column_type(WrenDataSource.bigquery, "INT64") == "integer"


def test_bigquery_string():
    assert map_column_type(WrenDataSource.bigquery, "STRING") == "varchar"


def test_bigquery_bool():
    assert map_column_type(WrenDataSource.bigquery, "BOOL") == "boolean"


def test_duckdb_integer():
    assert map_column_type(WrenDataSource.duckdb, "INTEGER") == "integer"


def test_duckdb_varchar():
    assert map_column_type(WrenDataSource.duckdb, "VARCHAR") == "varchar"


def test_mssql_int():
    assert map_column_type(WrenDataSource.mssql, "int") == "integer"


def test_mssql_nvarchar():
    assert map_column_type(WrenDataSource.mssql, "nvarchar") == "varchar"


def test_mysql_timestamp():
    assert map_column_type(WrenDataSource.mysql, "TIMESTAMP") == "TIMESTAMPTZ"


def test_postgres_passthrough():
    # Postgres types are passed through lowercased
    assert map_column_type(WrenDataSource.postgres, "text") == "text"
    assert map_column_type(WrenDataSource.postgres, "UUID") == "uuid"
