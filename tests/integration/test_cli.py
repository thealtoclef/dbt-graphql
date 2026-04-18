import json
from pathlib import Path

import pytest

from dbt_mdl.cli import main

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


def _cli_wren_args():
    return [
        "generate", "wren",
        "--catalog", str(CATALOG),
        "--manifest", str(MANIFEST),
    ]


def test_cli_produces_output_files(tmp_path):
    output_dir = tmp_path / "out"
    main(_cli_wren_args() + ["--output", str(output_dir)])
    assert (output_dir / "mdl.json").exists()


def test_cli_mdl_json_valid(tmp_path):
    output_dir = tmp_path / "out"
    main(_cli_wren_args() + ["--output", str(output_dir)])
    data = json.loads((output_dir / "mdl.json").read_text())
    assert "models" in data
    assert "relationships" in data
    assert any(m["name"] == "customers" for m in data["models"])


def test_cli_data_source_is_duckdb(tmp_path):
    output_dir = tmp_path / "out"
    main(_cli_wren_args() + ["--output", str(output_dir)])
    data = json.loads((output_dir / "mdl.json").read_text())
    assert data.get("dataSource") == "duckdb"


def test_cli_all_models_included_by_default(tmp_path):
    output_dir = tmp_path / "out"
    main(_cli_wren_args() + ["--output", str(output_dir)])
    data = json.loads((output_dir / "mdl.json").read_text())
    model_names = [m["name"] for m in data["models"]]
    assert "stg_orders" in model_names


def test_cli_exclude_single_pattern(tmp_path):
    output_dir = tmp_path / "out"
    main(
        _cli_wren_args()
        + ["--exclude", "^stg_", "--output", str(output_dir)]
    )
    data = json.loads((output_dir / "mdl.json").read_text())
    model_names = [m["name"] for m in data["models"]]
    assert "stg_orders" not in model_names
    assert "customers" in model_names


def test_cli_exclude_multiple_patterns(tmp_path):
    output_dir = tmp_path / "out"
    main(
        _cli_wren_args()
        + [
            "--exclude", "^stg_",
            "--exclude", "^ord",
            "--output", str(output_dir),
        ]
    )
    data = json.loads((output_dir / "mdl.json").read_text())
    model_names = [m["name"] for m in data["models"]]
    assert "stg_orders" not in model_names
    assert "orders" not in model_names
    assert "customers" in model_names


def test_cli_no_format_exits():
    with pytest.raises(SystemExit):
        main(["generate", "--catalog", "/dev/null", "--manifest", "/dev/null"])


def test_cli_graphql_generates_db_graphql(tmp_path):
    output_dir = tmp_path / "out"
    main([
        "generate", "graphql",
        "--catalog", str(CATALOG),
        "--manifest", str(MANIFEST),
        "--output", str(output_dir),
    ])
    assert (output_dir / "db.graphql").exists()


def test_cli_default_output_is_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    main(_cli_wren_args())
    assert (tmp_path / "mdl.json").exists()
