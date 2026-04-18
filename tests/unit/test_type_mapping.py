from dbt_mdl.wren.type_mapping import map_column_type


def test_bigquery_int64():
    assert map_column_type("bigquery", "INT64") == "integer"


def test_bigquery_string():
    assert map_column_type("bigquery", "STRING") == "varchar"


def test_bigquery_bool():
    assert map_column_type("bigquery", "BOOL") == "boolean"


def test_duckdb_integer():
    assert map_column_type("duckdb", "INTEGER") == "integer"


def test_duckdb_varchar():
    assert map_column_type("duckdb", "VARCHAR") == "varchar"


def test_mssql_int():
    assert map_column_type("mssql", "int") == "integer"


def test_mssql_nvarchar():
    assert map_column_type("mssql", "nvarchar") == "varchar"


def test_mysql_timestamp():
    assert map_column_type("mysql", "TIMESTAMP") == "timestamptz"


def test_postgres_passthrough():
    # Postgres types are passed through lowercased
    assert map_column_type("postgres", "text") == "text"
    assert map_column_type("postgres", "UUID") == "uuid"
