"""SQLAlchemy 2.0 async engine manager.

Connection info comes from our own config YAML (or a raw SQLAlchemy URL).
No dbt profiles are involved — the dbt profiles parser has been removed from
this codebase entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


# ---------------------------------------------------------------------------
# Config → SQLAlchemy URL
# ---------------------------------------------------------------------------

# Maps our config ``type:`` values to SQLAlchemy async driver schemes.
_DRIVER_MAP: dict[str, str] = {
    "mysql": "mysql+aiomysql",
    "mariadb": "mysql+aiomysql",
    "doris": "mysql+aiomysql",
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "sqlite": "sqlite+aiosqlite",
}


def build_db_url(config: dict[str, Any]) -> str:
    """Build a SQLAlchemy async URL from a config dict.

    Expected keys (matches our generated ``db.yml`` format)::

        type: mysql        # or postgres, sqlite, doris, mariadb
        host: localhost
        port: 3306
        dbname: mydb
        user: root
        password: secret

    For SQLite::

        type: sqlite
        host: /path/to/file.db
    """
    db_type = config.get("type", "").lower()
    scheme = _DRIVER_MAP.get(db_type)
    if scheme is None:
        supported = sorted(_DRIVER_MAP)
        raise ValueError(
            f"Unsupported database type '{db_type}'. Supported: {', '.join(supported)}"
        )

    if db_type == "sqlite":
        path = config.get("host", ":memory:")
        return f"{scheme}:///{path}"

    host = config.get("host", "localhost")
    port = config.get("port")
    dbname = config.get("dbname", "")
    user = config.get("user", "")
    password = config.get("password", "")

    auth = f"{user}:{password}" if password else user
    if port:
        return f"{scheme}://{auth}@{host}:{port}/{dbname}"
    return f"{scheme}://{auth}@{host}/{dbname}"


def load_db_config(path: str | Path) -> dict[str, Any]:
    """Load a ``db.yml`` config file and return the parsed dict."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("db config YAML must be a mapping")
    return data


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """Thin wrapper around a SQLAlchemy async engine.

    Initialise with either:
    - a raw SQLAlchemy URL string
    - a config dict (passed to ``build_db_url``)
    """

    def __init__(self, db_url: str | None = None, *, config: dict[str, Any] | None = None) -> None:
        if config and not db_url:
            db_url = build_db_url(config)
        if not db_url:
            raise ValueError("Provide either db_url or config")
        self._url = db_url
        self._engine: AsyncEngine | None = None

    async def connect(self) -> None:
        self._engine = create_async_engine(self._url)

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    async def execute(self, query) -> list[dict]:  # noqa: ANN001
        """Execute a SQLAlchemy Core selectable and return rows as dicts."""
        if self._engine is None:
            raise RuntimeError("DatabaseManager is not connected")
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            return [dict(row._mapping) for row in result]

    async def execute_text(self, sql: str) -> list[dict]:
        """Execute a raw SQL string and return rows as dicts."""
        if self._engine is None:
            raise RuntimeError("DatabaseManager is not connected")
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [dict(row._mapping) for row in result]

    @property
    def dialect_name(self) -> str:
        if self._engine is None:
            return ""
        return self._engine.dialect.name
