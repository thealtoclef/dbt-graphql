import os

import pytest
from pathlib import Path

from dbt_graphql.cli import main

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


def _config(tmp_path: Path, exclude: list[str] | None = None) -> Path:
    """Write a minimal config.yml with dbt paths pointing at fixtures."""
    lines = [
        "dbt:",
        f"  catalog: {CATALOG}",
        f"  manifest: {MANIFEST}",
    ]
    if exclude:
        lines.append("  exclude:")
        for p in exclude:
            lines.append(f"    - '{p}'")
    cfg = tmp_path / "config.yml"
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


# ---------------------------------------------------------------------------
# Generate mode (--output)
# ---------------------------------------------------------------------------


def test_cli_produces_db_graphql(tmp_path):
    cfg = _config(tmp_path)
    main(["--config", str(cfg), "--output", str(tmp_path)])
    assert (tmp_path / "db.graphql").exists()


def test_cli_all_models_included_by_default(tmp_path):
    cfg = _config(tmp_path)
    main(["--config", str(cfg), "--output", str(tmp_path)])
    assert "type stg_orders" in (tmp_path / "db.graphql").read_text()


def test_cli_exclude_single_pattern(tmp_path):
    cfg = _config(tmp_path, exclude=["^stg_"])
    main(["--config", str(cfg), "--output", str(tmp_path)])
    content = (tmp_path / "db.graphql").read_text()
    assert "type stg_orders" not in content
    assert "type customers" in content


def test_cli_exclude_multiple_patterns(tmp_path):
    cfg = _config(tmp_path, exclude=["^stg_", "^ord"])
    main(["--config", str(cfg), "--output", str(tmp_path)])
    content = (tmp_path / "db.graphql").read_text()
    assert "type stg_orders" not in content
    assert "type orders" not in content
    assert "type customers" in content


def test_cli_emits_lineage_directives_in_db_graphql(tmp_path):
    cfg = _config(tmp_path)
    main(["--config", str(cfg), "--output", str(tmp_path)])
    sdl = (tmp_path / "db.graphql").read_text()
    assert "@lineage(sources:" in sdl
    assert "@lineage(source:" in sdl


def test_cli_missing_catalog_exits_nonzero(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        f"dbt:\n  catalog: {tmp_path / 'no_catalog.json'}\n  manifest: {MANIFEST}\n"
    )
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "--output", str(tmp_path)])
    assert exc_info.value.code != 0


def test_cli_missing_manifest_exits_nonzero(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        f"dbt:\n  catalog: {CATALOG}\n  manifest: {tmp_path / 'no_manifest.json'}\n"
    )
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "--output", str(tmp_path)])
    assert exc_info.value.code != 0


def test_cli_no_config_no_env_exits_nonzero(monkeypatch):
    """Without --config and without DBT_GRAPHQL__DBT__* env vars, AppConfig
    cannot be built (dbt is required) and the CLI must exit nonzero."""
    for var in [v for v in os.environ if v.startswith("DBT_GRAPHQL")]:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code != 0


def test_cli_invalid_config_exits_nonzero(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("not_a_mapping: - oops\n")
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "--output", str(tmp_path)])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Env-only configuration (pydantic-settings)
# ---------------------------------------------------------------------------


def test_cli_runs_from_env_vars_only(tmp_path, monkeypatch):
    """Without --config, configuration is sourced from DBT_GRAPHQL__* env vars."""
    monkeypatch.setenv("DBT_GRAPHQL__DBT__CATALOG", str(CATALOG))
    monkeypatch.setenv("DBT_GRAPHQL__DBT__MANIFEST", str(MANIFEST))
    main(["--output", str(tmp_path / "out")])
    assert (tmp_path / "out" / "db.graphql").exists()


# ---------------------------------------------------------------------------
# Serve mode (config-driven)
# ---------------------------------------------------------------------------


def test_cli_warns_in_dev_mode(tmp_path, monkeypatch, capsys):
    """``dev_mode: true`` bypasses authn/authz; the server logs a warning."""
    from dbt_graphql import cli as cli_mod
    from dbt_graphql.compiler import connection as conn_mod
    import uvicorn

    def _fake_uvicorn_run(**_kw):
        raise SystemExit(0)

    monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setattr(conn_mod, "DatabaseManager", lambda **_kw: None)

    config_file = tmp_path / "config.yml"
    config_file.write_text(
        f"dbt:\n  catalog: {CATALOG}\n  manifest: {MANIFEST}\n"
        "dev_mode: true\n"
        "db:\n  type: postgres\n  host: localhost\n  dbname: test\n"
        "serve:\n  host: 0.0.0.0\n  port: 8080\n"
    )
    with pytest.raises(SystemExit):
        cli_mod.main(["--config", str(config_file)])

    err = capsys.readouterr().err
    assert "dev_mode=true" in err
