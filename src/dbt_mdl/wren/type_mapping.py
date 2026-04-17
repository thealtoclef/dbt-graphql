"""Map raw database column types to Wren MDL type strings."""


def map_column_type(data_source: str, source_type: str) -> str:
    """Map a database-native column type to a Wren MDL type string."""
    upper = source_type.upper().strip()

    if data_source == "bigquery":
        return _map_bigquery_type(upper)
    elif data_source == "duckdb":
        return _map_duckdb_type(upper)
    elif data_source == "mssql":
        return _map_mssql_type(source_type.lower().strip())
    elif data_source == "mysql":
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
            return "tinyint"
        case "SMALLINT":
            return "smallint"
        case "MEDIUMINT" | "INT" | "INTEGER":
            return "integer"
        case "BIGINT":
            return "bigint"
        case "FLOAT" | "DOUBLE":
            return "double"
        case "DECIMAL" | "NUMERIC":
            return "decimal"
        case "DATE":
            return "date"
        case "DATETIME":
            return "datetime"
        case "TIMESTAMP":
            return "timestamptz"
        case "BOOLEAN" | "BOOL":
            return "boolean"
        case "JSON":
            return "json"
        case _:
            return upper.lower()
