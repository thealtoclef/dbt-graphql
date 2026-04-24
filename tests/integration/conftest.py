"""Shared fixtures for integration tests across PostgreSQL and MySQL."""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

FIXTURES = Path(__file__).parent.parent / "fixtures"
JAFFLE = FIXTURES / "jaffle-shop"
PROFILES_DIR = FIXTURES / "dbt-profiles"

_ADAPTER_PKG = {
    "postgres": "dbt-postgres",
    "mysql": "dbt-mysql",
}

_DRIVER_MODULE = {
    "postgres": "asyncpg",
    "mysql": "aiomysql",
}

_ASYNC_URL = {
    "postgres": "postgresql+asyncpg://jaffle:jaffle@localhost:5433/jaffle_shop",
    "mysql": "mysql+aiomysql://jaffle:jaffle@localhost:3307/jaffle_shop",
}


# ---------------------------------------------------------------------------
# DB adapter wrappers
# ---------------------------------------------------------------------------


class AsyncDBConnection:
    """Async DatabaseManager wrapper (PostgreSQL, MySQL)."""

    def __init__(self, db_url: str) -> None:
        from dbt_graphql.compiler.connection import DatabaseManager

        self._mgr = DatabaseManager(db_url=db_url)
        self._url = db_url

    @property
    def dialect_name(self) -> str:
        scheme = self._url.split("://")[0]
        return scheme.split("+")[0]

    async def connect(self) -> None:
        await self._mgr.connect()

    async def execute(self, query) -> list[dict[str, object]]:
        return await self._mgr.execute(query)

    async def execute_text(self, sql: str) -> list[dict[str, object]]:
        return await self._mgr.execute_text(sql)

    async def close(self) -> None:
        await self._mgr.close()


# ---------------------------------------------------------------------------
# Adapter environment bundle
# ---------------------------------------------------------------------------


class AdapterEnv:
    """Bundles project info + DB connection + registry for one adapter."""

    def __init__(
        self,
        name: str,
        project,
        db,
        registry,
        db_graphql: str,
    ) -> None:
        self.name = name
        self.project = project
        self.db = db
        self.registry = registry
        self.db_graphql = db_graphql

    @property
    def db_url(self) -> str | None:
        return _ASYNC_URL.get(self.name)


# ---------------------------------------------------------------------------
# Cached derived data (project + registry + graphql per adapter)
# ---------------------------------------------------------------------------

_adapter_cache: dict[str, dict] = {}


def _get_adapter_data(name: str, artifacts: dict) -> dict:
    if name not in _adapter_cache:
        from dbt_graphql.formatter.graphql import format_graphql
        from dbt_graphql.formatter.schema import parse_db_graphql
        from dbt_graphql.pipeline import extract_project

        project = extract_project(artifacts["catalog_path"], artifacts["manifest_path"])
        result = format_graphql(project)
        _, registry = parse_db_graphql(result.db_graphql)
        _adapter_cache[name] = {
            "project": project,
            "registry": registry,
            "db_graphql": result.db_graphql,
        }
    return _adapter_cache[name]


# ---------------------------------------------------------------------------
# Docker fixtures (pytest-docker)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_compose_file():
    return str(FIXTURES / "docker-compose.yml")


def _tcp_check(host: str, port: int):
    def check():
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    return check


@pytest.fixture(scope="session")
def postgres_service(docker_services):
    docker_services.wait_until_responsive(
        timeout=30.0, pause=1.0, check=_tcp_check("localhost", 5433)
    )


@pytest.fixture(scope="session")
def mysql_service(docker_services):
    docker_services.wait_until_responsive(
        timeout=30.0, pause=1.0, check=_tcp_check("localhost", 3307)
    )


# ---------------------------------------------------------------------------
# dbt project building
# ---------------------------------------------------------------------------


def _create_dbt_venv(venv_dir: Path, adapter: str) -> Path:
    if (venv_dir / "bin" / "dbt").exists():
        return venv_dir / "bin" / "dbt"

    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", "3.13"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_dir / "bin" / "python"),
            "dbt-core",
            _ADAPTER_PKG[adapter],
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return venv_dir / "bin" / "dbt"


def _run_dbt(dbt_bin: Path, project_dir: Path, profiles_dir: Path) -> None:
    env = {"DBT_PROFILES_DIR": str(profiles_dir)}
    for cmd in [
        [str(dbt_bin), "build", "--full-refresh"],
        [str(dbt_bin), "docs", "generate"],
    ]:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.fail(
                f"dbt {cmd[1]} failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )


def _build_dbt_project(
    adapter: str, venvs_dir: Path, projects_dir: Path
) -> dict[str, Path]:
    dbt_bin = _create_dbt_venv(venvs_dir / adapter, adapter)

    project_dir = projects_dir / adapter
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(JAFFLE, project_dir)

    profiles_subdir = project_dir / "profiles"
    profiles_subdir.mkdir(exist_ok=True)
    shutil.copy(PROFILES_DIR / f"{adapter}.yml", profiles_subdir / "profiles.yml")

    _run_dbt(dbt_bin, project_dir, profiles_subdir)

    target = project_dir / "target"
    return {
        "catalog_path": target / "catalog.json",
        "manifest_path": target / "manifest.json",
        "project_dir": project_dir,
    }


# ---------------------------------------------------------------------------
# Session-scoped artifact fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_artifacts(tmp_path_factory, postgres_service):
    return _build_dbt_project(
        "postgres",
        tmp_path_factory.mktemp("dbt-envs"),
        tmp_path_factory.mktemp("e2e"),
    )


@pytest.fixture(scope="session")
def mysql_artifacts(tmp_path_factory, mysql_service):
    return _build_dbt_project(
        "mysql",
        tmp_path_factory.mktemp("dbt-envs"),
        tmp_path_factory.mktemp("e2e"),
    )


# ---------------------------------------------------------------------------
# Parametrized test fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(params=["postgres", "mysql"])
async def adapter_env(request, tmp_path):
    """Parametrized fixture providing AdapterEnv for each adapter."""
    name = request.param
    pytest.importorskip(_DRIVER_MODULE[name])

    artifacts = request.getfixturevalue(f"{name}_artifacts")
    data = _get_adapter_data(name, artifacts)

    db = AsyncDBConnection(_ASYNC_URL[name])
    await db.connect()

    yield AdapterEnv(
        name=name,
        project=data["project"],
        db=db,
        registry=data["registry"],
        db_graphql=data["db_graphql"],
    )
    await db.close()


@pytest.fixture(params=["postgres", "mysql"])
def serve_adapter_env(request, tmp_path):
    """Parametrized fixture for HTTP serve tests."""
    name = request.param
    pytest.importorskip(_DRIVER_MODULE[name])

    artifacts = request.getfixturevalue(f"{name}_artifacts")
    data = _get_adapter_data(name, artifacts)

    gql_path = tmp_path / f"db_{name}.graphql"
    gql_path.write_text(data["db_graphql"])

    yield {
        "name": name,
        "db_url": _ASYNC_URL[name],
        "db_graphql_path": gql_path,
    }
