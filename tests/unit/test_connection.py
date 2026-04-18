"""Tests for the connection manager (connection.py)."""

import pytest

from dbt_mdl.graphql.connection import build_db_url, DatabaseManager


class TestBuildDbUrl:
    def test_mysql(self):
        url = build_db_url({
            "type": "mysql",
            "host": "localhost",
            "port": 3306,
            "dbname": "mydb",
            "user": "root",
            "password": "secret",
        })
        assert url == "mysql+aiomysql://root:secret@localhost:3306/mydb"

    def test_mysql_no_password(self):
        url = build_db_url({
            "type": "mysql",
            "host": "localhost",
            "dbname": "mydb",
            "user": "root",
        })
        assert url == "mysql+aiomysql://root@localhost/mydb"

    def test_sqlite(self):
        url = build_db_url({"type": "sqlite", "host": "/tmp/test.db"})
        assert url == "sqlite+aiosqlite:////tmp/test.db"

    def test_sqlite_memory(self):
        url = build_db_url({"type": "sqlite"})
        assert url == "sqlite+aiosqlite:///:memory:"

    def test_doris_maps_to_mysql(self):
        url = build_db_url({
            "type": "doris",
            "host": "doris.example.com",
            "port": 9030,
            "dbname": "analytics",
            "user": "admin",
            "password": "pw",
        })
        assert url.startswith("mysql+aiomysql://")

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            build_db_url({"type": "cassandra"})


class TestDatabaseManager:
    def test_init_with_url(self):
        db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
        assert db._url == "sqlite+aiosqlite:///:memory:"

    def test_init_with_config(self):
        db = DatabaseManager(config={"type": "sqlite", "host": ":memory:"})
        assert "sqlite+aiosqlite" in db._url

    def test_init_without_url_or_config_raises(self):
        with pytest.raises(ValueError, match="Provide either"):
            DatabaseManager()

    @pytest.mark.asyncio
    async def test_connect_and_close(self):
        aiosqlite = pytest.importorskip("aiosqlite")
        db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
        await db.connect()
        assert db.dialect_name == "sqlite"
        await db.close()

    @pytest.mark.asyncio
    async def test_execute_without_connect_raises(self):
        db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
        with pytest.raises(RuntimeError, match="not connected"):
            await db.execute_text("SELECT 1")
