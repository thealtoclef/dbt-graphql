"""Shared fixtures for GraphQL integration tests.

Uses the jaffle-shop dbt project with duckdb adapter.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

FIXTURES = Path(__file__).parent.parent / "fixtures"
JAFFLE = FIXTURES / "jaffle-shop"
PROFILES_DIR = FIXTURES / "dbt-profiles"


# ---------------------------------------------------------------------------
# Sync DB wrapper for duckdb
# ---------------------------------------------------------------------------


class SyncDBConnection:
    """Wraps a sync SQLAlchemy engine for async use via run_in_executor."""

    def __init__(self, url: str):
        from sqlalchemy import create_engine

        self._engine = create_engine(url)

    async def execute(self, query) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()

        def _sync_exec():
            with self._engine.connect() as conn:
                result = conn.execute(query)
                return [dict(row._mapping) for row in result]

        return await loop.run_in_executor(None, _sync_exec)

    async def close(self):
        self._engine.dispose()


# ---------------------------------------------------------------------------
# Isolated dbt venv for duckdb
# ---------------------------------------------------------------------------


def _create_dbt_venv(venv_dir: Path) -> Path:
    """Create an isolated venv with dbt-core + dbt-duckdb. Returns dbt bin path."""
    if (venv_dir / "bin" / "dbt").exists():
        return venv_dir / "bin" / "dbt"

    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", "3.13"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_dir / "bin" / "python"),
         "dbt-core", "dbt-duckdb"],
        check=True, capture_output=True, text=True,
        timeout=300,
    )
    return venv_dir / "bin" / "dbt"


def _run_dbt(dbt_bin: Path, project_dir: Path, profiles_dir: Path) -> None:
    """Run dbt seed + run + docs generate using the adapter-specific dbt."""
    env = {"DBT_PROFILES_DIR": str(profiles_dir)}
    for cmd in [
        [str(dbt_bin), "seed", "--full-refresh"],
        [str(dbt_bin), "run", "--full-refresh"],
        [str(dbt_bin), "docs", "generate"],
    ]:
        result = subprocess.run(
            cmd, cwd=str(project_dir), capture_output=True,
            text=True, env=env, timeout=120,
        )
        if result.returncode != 0:
            pytest.fail(
                f"dbt {cmd[1]} failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )


def _build_duckdb_project(venvs_dir: Path, projects_dir: Path) -> dict[str, Path]:
    """Create venv, copy project, run dbt, return artifact paths."""
    dbt_bin = _create_dbt_venv(venvs_dir / "duckdb")

    project_dir = projects_dir / "duckdb"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(JAFFLE, project_dir)

    profiles_subdir = project_dir / "profiles"
    profiles_subdir.mkdir(exist_ok=True)
    shutil.copy(PROFILES_DIR / "duckdb.yml", profiles_subdir / "profiles.yml")

    _run_dbt(dbt_bin, project_dir, profiles_subdir)

    target = project_dir / "target"
    return {
        "catalog_path": target / "catalog.json",
        "manifest_path": target / "manifest.json",
        "project_dir": project_dir,
    }


# ---------------------------------------------------------------------------
# Session-scoped: build duckdb project
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def duckdb_project(tmp_path_factory):
    return _build_duckdb_project(
        tmp_path_factory.mktemp("dbt-envs"),
        tmp_path_factory.mktemp("e2e"),
    )


# ---------------------------------------------------------------------------
# Dict-style fixtures for parametrized tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def dbt_artifacts(duckdb_project):
    """Returns {adapter_name: {catalog_path, manifest_path, project_dir}}."""
    return {"duckdb": duckdb_project}


@pytest_asyncio.fixture
async def db_connection(dbt_artifacts):
    """Returns {adapter_name: connection} — fresh connections per test."""
    connections: dict[str, SyncDBConnection] = {}

    duckdb_path = dbt_artifacts["duckdb"]["project_dir"] / "jaffle_shop.duckdb"
    connections["duckdb"] = SyncDBConnection(f"duckdb:///{duckdb_path}")

    yield connections

    for db in connections.values():
        await db.close()
