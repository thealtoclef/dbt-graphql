import json
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


def test_cli_produces_lineage_json(tmp_path):
    cfg = _config(tmp_path)
    main(["--config", str(cfg), "--output", str(tmp_path)])
    assert (tmp_path / "lineage.json").exists()


def test_cli_lineage_json_has_table_lineage(tmp_path):
    cfg = _config(tmp_path)
    main(["--config", str(cfg), "--output", str(tmp_path)])
    data = json.loads((tmp_path / "lineage.json").read_text())
    assert "tableLineage" in data or "table_lineage" in data


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


def test_cli_no_config_shows_help():
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 0


def test_cli_invalid_config_exits_nonzero(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("not_a_mapping: - oops\n")
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "--output", str(tmp_path)])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Relative path resolution in config
# ---------------------------------------------------------------------------


def test_cli_relative_paths_resolved_from_config_dir(tmp_path):
    """catalog/manifest given as relative paths must resolve from the config file's directory."""
    import shutil

    shutil.copy(CATALOG, tmp_path / "catalog.json")
    shutil.copy(MANIFEST, tmp_path / "manifest.json")
    cfg = tmp_path / "config.yml"
    cfg.write_text("dbt:\n  catalog: catalog.json\n  manifest: manifest.json\n")
    main(["--config", str(cfg), "--output", str(tmp_path / "out")])
    assert (tmp_path / "out" / "db.graphql").exists()


# ---------------------------------------------------------------------------
# Serve mode (config-driven)
# ---------------------------------------------------------------------------


def test_env_var_overrides_enrichment_budget(monkeypatch, tmp_path):
    """DBT_GRAPHQL__ENRICHMENT__BUDGET env var must override config.yml enrichment.budget."""
    import dbt_graphql.compiler.connection as conn_mod
    import dbt_graphql.mcp.server as mcp_server_mod
    import granian as granian_mod

    captured = {}

    def _fake_create_mcp_http_app(_project, *, enrichment=None, **_kwargs):
        captured["enrichment"] = enrichment
        return object()

    class _FakeGranian:
        def __init__(self, **_kw):
            pass

        def serve(self):
            raise SystemExit(0)

    monkeypatch.setattr(
        mcp_server_mod, "create_mcp_http_app", _fake_create_mcp_http_app
    )
    monkeypatch.setattr(granian_mod, "Granian", _FakeGranian)
    monkeypatch.setattr(conn_mod, "DatabaseManager", lambda **_kw: None)
    monkeypatch.setenv("DBT_GRAPHQL__ENRICHMENT__BUDGET", "7")

    config_file = tmp_path / "config.yml"
    config_file.write_text(
        f"dbt:\n  catalog: {CATALOG}\n  manifest: {MANIFEST}\n"
        "db:\n  type: postgres\n  host: localhost\n  dbname: test\n"
        "serve:\n  host: 0.0.0.0\n  port: 8080\n"
        "  mcp:\n    enabled: true\n"
        "security:\n  allow_anonymous: true\n"
        "enrichment:\n  budget: 100\n"
    )

    with pytest.raises(SystemExit):
        main(["--config", str(config_file)])

    assert captured["enrichment"].budget == 7


def test_cli_refuses_serve_when_jwt_disabled_without_allow_anonymous(
    tmp_path, capsys
):
    """The serve command must refuse to start when JWT verification is off
    and the operator has not explicitly opted into anonymous access. This
    closes the "forgot to enable JWT in prod" footgun."""
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        f"dbt:\n  catalog: {CATALOG}\n  manifest: {MANIFEST}\n"
        "db:\n  type: postgres\n  host: localhost\n  dbname: test\n"
        "serve:\n  host: 0.0.0.0\n  port: 8080\n"
        "  graphql:\n    enabled: true\n"
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_file)])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "allow_anonymous" in err
