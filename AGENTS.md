## Project Context
- **Purpose**: Convert dbt projects into a typed GraphQL API and MCP surface for LLM agents. Reads `catalog.json` and `manifest.json` from dbt to derive everything automatically.
- **Tech Stack**: Python 3.11, Starlette, Ariadne (GraphQL), SQLAlchemy 2.0 (async), FastMCP.
- **Package Manager**: Use `uv` for all dependency and environment management.

## Dev Environment
- **Setup**: `uv sync --all-extras --all-groups`
- **Virtual Env**: `.venv/` (AI should use the interpreter in this directory).
- **Configuration**: Environment variables are prefixed `DBT_GRAPHQL__` (see `config.example.yml`).

## Commands You Can Use
- **Run Tests**: `uv run pytest` (all) or `uv run pytest tests/unit/` (unit only)
- **Lint Code**: `uv run ruff check --fix`
- **Format Code**: `uv run ruff format`
- **Type Check**: `uv run ty check --fix`

## Coding Standards & Patterns
- **Typing**: Strict type hints required. Use `from __future__ import annotations` everywhere.
- **Config**: Pydantic models in `config.py`. Use `pydantic-settings` for env var precedence (`DBT_GRAPHQL__*`).
- **Async**: Use `async`/`await` for all I/O and database operations.
- **Logging**: Use `loguru` (logger) instead of `print()`.

## Project Structure
- `src/dbt_graphql/` — Core package
- `tests/unit/` — Isolated unit tests (no external dependencies)
- `tests/integration/` — Integration tests (require Docker: PostgreSQL, MySQL, Redis)
- `tests/fixtures/` — Test fixtures (dbt artifacts, docker-compose, jaffle-shop sample)
- `docs/` — Architecture and design documentation

## Boundaries & Safety
- ✅ **Always do**: Run `uv run ruff check --fix && uv run ruff format` before finishing a task.
- ⚠️ **Ask first**: Before adding new top-level dependencies to `pyproject.toml`.
- 🚫 **Never do**: Delete files in `tests/fixtures/dbt-artifacts/` or `tests/fixtures/jaffle-shop/`.
- 🚫 **Never do**: Modify `tests/fixtures/docker-compose.yml` — it defines shared test infrastructure.
- 🚫 **Never do**: Modify `.env.example` or create new `.env` files directly; use `config.example.yml` as template.
