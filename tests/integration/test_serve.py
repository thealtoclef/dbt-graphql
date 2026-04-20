"""Integration tests for the GraphQL HTTP server (FastAPI + Ariadne + SQLite).

Creates a real SQLite database, generates a minimal db.graphql schema, starts
the FastAPI app via Starlette's TestClient (which handles the ASGI lifespan),
and makes real HTTP GraphQL requests to verify the full request path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from starlette.testclient import TestClient

from dbt_graphql.serve.app import create_app

pytest.importorskip("aiosqlite", reason="aiosqlite required for SQLite async tests")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sqlite_db(tmp_path_factory) -> Path:
    """SQLite database with two tables and seed data."""
    db_path = tmp_path_factory.mktemp("serve_db") / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(
            text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        )
        conn.execute(
            text(
                "CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT)"
            )
        )
        conn.execute(text("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob')"))
        conn.execute(
            text(
                "INSERT INTO posts VALUES (1, 1, 'Hello'), (2, 1, 'World'), (3, 2, 'Hi')"
            )
        )
        conn.commit()
    engine.dispose()
    return db_path


@pytest.fixture(scope="module")
def db_graphql_file(tmp_path_factory) -> Path:
    """Minimal db.graphql SDL matching the SQLite test database."""
    path = tmp_path_factory.mktemp("serve_schema") / "db.graphql"
    path.write_text(
        'type users @database(name: "test") @schema(name: "main") @table(name: "users") {\n'
        '  id: Int! @sql(type: "INTEGER") @id\n'
        '  name: String! @sql(type: "TEXT")\n'
        "}\n"
        "\n"
        'type posts @database(name: "test") @schema(name: "main") @table(name: "posts") {\n'
        '  id: Int! @sql(type: "INTEGER") @id\n'
        '  user_id: Int @sql(type: "INTEGER")\n'
        '  title: String @sql(type: "TEXT")\n'
        "}\n"
    )
    return path


@pytest.fixture(scope="module")
def client(sqlite_db, db_graphql_file) -> TestClient:
    """TestClient wrapping the real FastAPI app; handles ASGI lifespan."""
    app = create_app(
        db_graphql_path=db_graphql_file,
        db_url=f"sqlite+aiosqlite:///{sqlite_db}",
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraphQLHTTP:
    def test_query_all_users(self, client):
        resp = client.post("/graphql", json={"query": "{ users { id name } }"})
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data
        rows = data["data"]["users"]
        assert len(rows) == 2
        names = {r["name"] for r in rows}
        assert "Alice" in names
        assert "Bob" in names

    def test_query_all_posts(self, client):
        resp = client.post("/graphql", json={"query": "{ posts { id title } }"})
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data
        assert len(data["data"]["posts"]) == 3

    def test_query_with_limit(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ users(limit: 1) { id name } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data
        assert len(data["data"]["users"]) == 1

    def test_query_selected_fields_only(self, client):
        resp = client.post("/graphql", json={"query": "{ users { name } }"})
        assert resp.status_code == 200
        data = resp.json()
        rows = data["data"]["users"]
        assert all("name" in r for r in rows)
        # id not requested → not in result
        assert all("id" not in r for r in rows)

    def test_invalid_graphql_syntax_returns_error(self, client):
        resp = client.post("/graphql", json={"query": "{ not valid graphql {{{"})
        # Ariadne returns 400 for syntax errors
        assert resp.status_code in (400, 200)
        data = resp.json()
        if resp.status_code == 200:
            assert "errors" in data

    def test_introspection_type_names(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ __schema { types { name } } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        type_names = {t["name"] for t in data["data"]["__schema"]["types"]}
        assert "users" in type_names
        assert "posts" in type_names

    def test_schema_exposes_where_input_types(self, client):
        """Each table must have a WhereInput type so the where argument is usable."""
        resp = client.post(
            "/graphql",
            json={"query": "{ __schema { types { name } } }"},
        )
        assert resp.status_code == 200
        type_names = {t["name"] for t in resp.json()["data"]["__schema"]["types"]}
        assert "usersWhereInput" in type_names
        assert "postsWhereInput" in type_names

    def test_where_filter_end_to_end(self, client):
        """where arg must reach the SQL engine and filter rows correctly."""
        resp = client.post(
            "/graphql",
            json={"query": "{ users(where: { id: 1 }) { id name } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        rows = data["data"]["users"]
        assert len(rows) == 1
        assert rows[0]["id"] == 1
        assert rows[0]["name"] == "Alice"

    def test_where_filter_no_match_returns_empty(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ users(where: { id: 999 }) { id name } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert data["data"]["users"] == []
