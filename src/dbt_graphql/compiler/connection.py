"""SQLAlchemy 2.0 async engine manager.

Connection info comes from our own config YAML (or a raw SQLAlchemy URL).
No dbt profiles are involved — the dbt profiles parser has been removed from
this codebase entirely.
"""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..config import DbConfig, PoolConfig

_meter = metrics.get_meter(__name__)
_pool_wait_hist = _meter.create_histogram(
    name="db.client.connections.wait_time",
    description="Time spent waiting on pool checkout (ms)",
    unit="ms",
)


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
}


def build_db_url(config: DbConfig | dict[str, Any]) -> str:
    """Build a SQLAlchemy async URL from a ``DbConfig`` or equivalent dict."""
    if isinstance(config, dict):
        config = DbConfig.model_validate(config)

    db_type = config.type.lower()
    scheme = _DRIVER_MAP.get(db_type)
    if scheme is None:
        supported = sorted(_DRIVER_MAP)
        raise ValueError(
            f"Unsupported database type '{db_type}'. Supported: {', '.join(supported)}"
        )

    auth = f"{config.user}:{config.password}" if config.password else config.user
    if config.port:
        return f"{scheme}://{auth}@{config.host}:{config.port}/{config.dbname}"
    return f"{scheme}://{auth}@{config.host}/{config.dbname}"


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """Thin wrapper around a SQLAlchemy async engine.

    Initialise with either:
    - a raw SQLAlchemy URL string
    - a config dict (passed to ``build_db_url``)
    """

    def __init__(
        self,
        db_url: str | None = None,
        *,
        config: DbConfig | None = None,
        pool: PoolConfig | None = None,
    ) -> None:
        if config and not db_url:
            db_url = build_db_url(config)
        if not db_url:
            raise ValueError("Provide either db_url or config")
        self._url = db_url
        self._pool = pool or (config.pool if config else PoolConfig())
        self._engine: AsyncEngine | None = None

    async def connect(self) -> None:
        self._engine = create_async_engine(
            self._url,
            pool_size=self._pool.size,
            max_overflow=self._pool.max_overflow,
            pool_timeout=self._pool.timeout,
            pool_recycle=self._pool.recycle,
        )

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    async def execute(self, query) -> list[dict]:  # noqa: ANN001
        """Execute a SQLAlchemy Core selectable and return rows as dicts."""
        if self._engine is None:
            raise RuntimeError("DatabaseManager is not connected")
        start = time.perf_counter()
        try:
            async with self._engine.connect() as conn:
                _pool_wait_hist.record(
                    (time.perf_counter() - start) * 1000, {"outcome": "acquired"}
                )
                result = await conn.execute(query)
                return [dict(row._mapping) for row in result]
        except SAPoolTimeoutError:
            _pool_wait_hist.record(
                (time.perf_counter() - start) * 1000, {"outcome": "timeout"}
            )
            raise

    async def execute_text(self, sql: str) -> list[dict]:
        """Execute a raw SQL string and return rows as dicts."""
        if self._engine is None:
            raise RuntimeError("DatabaseManager is not connected")
        start = time.perf_counter()
        try:
            async with self._engine.connect() as conn:
                _pool_wait_hist.record(
                    (time.perf_counter() - start) * 1000, {"outcome": "acquired"}
                )
                result = await conn.execute(text(sql))
                return [dict(row._mapping) for row in result]
        except SAPoolTimeoutError:
            _pool_wait_hist.record(
                (time.perf_counter() - start) * 1000, {"outcome": "timeout"}
            )
            raise

    @property
    def dialect_name(self) -> str:
        if self._engine is None:
            raise RuntimeError(
                "DatabaseManager is not connected — call connect() first"
            )
        return self._engine.dialect.name
