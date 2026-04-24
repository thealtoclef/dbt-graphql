"""Tests for the connection manager (connection.py)."""

import pytest

from dbt_graphql.compiler.connection import build_db_url, DatabaseManager
from dbt_graphql.config import DbConfig


class TestBuildDbUrl:
    def test_mysql(self):
        url = build_db_url(
            {
                "type": "mysql",
                "host": "localhost",
                "port": 3306,
                "dbname": "mydb",
                "user": "root",
                "password": "secret",
            }
        )
        assert url == "mysql+aiomysql://root:secret@localhost:3306/mydb"

    def test_mysql_no_password(self):
        url = build_db_url(
            {
                "type": "mysql",
                "host": "localhost",
                "dbname": "mydb",
                "user": "root",
            }
        )
        assert url == "mysql+aiomysql://root@localhost/mydb"

    def test_postgres(self):
        url = build_db_url(
            {
                "type": "postgres",
                "host": "localhost",
                "port": 5432,
                "dbname": "mydb",
                "user": "admin",
                "password": "pw",
            }
        )
        assert url == "postgresql+asyncpg://admin:pw@localhost:5432/mydb"

    def test_doris_maps_to_mysql(self):
        url = build_db_url(
            {
                "type": "doris",
                "host": "doris.example.com",
                "port": 9030,
                "dbname": "analytics",
                "user": "admin",
                "password": "pw",
            }
        )
        assert url.startswith("mysql+aiomysql://")

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            build_db_url({"type": "cassandra"})


class TestDatabaseManager:
    def test_init_with_url(self):
        db = DatabaseManager(db_url="mysql+aiomysql://root@localhost/mydb")
        assert db._url == "mysql+aiomysql://root@localhost/mydb"

    def test_init_with_config(self):
        db = DatabaseManager(
            config=DbConfig(type="mysql", host="localhost", user="root", dbname="mydb")
        )
        assert "mysql+aiomysql" in db._url

    def test_init_without_url_or_config_raises(self):
        with pytest.raises(ValueError, match="Provide either"):
            DatabaseManager()

    @pytest.mark.asyncio
    async def test_execute_without_connect_raises(self):
        db = DatabaseManager(db_url="mysql+aiomysql://root@localhost/mydb")
        with pytest.raises(RuntimeError, match="not connected"):
            await db.execute_text("SELECT 1")

    def test_dialect_name_without_connect_raises(self):
        db = DatabaseManager(db_url="mysql+aiomysql://root@localhost/mydb")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = db.dialect_name
