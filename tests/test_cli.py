import json
import shutil
from pathlib import Path

import pytest

from wren_dbt_converter.cli import main

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def dbt_project(tmp_path):
    (tmp_path / "target").mkdir()
    shutil.copy(FIXTURES_DIR / "catalog.json", tmp_path / "target" / "catalog.json")
    shutil.copy(FIXTURES_DIR / "manifest.json", tmp_path / "target" / "manifest.json")
    shutil.copy(FIXTURES_DIR / "profiles.yml", tmp_path / "profiles.yml")
    shutil.copy(FIXTURES_DIR / "dbt_project.yml", tmp_path / "dbt_project.yml")
    return tmp_path


def test_cli_produces_output_files(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main([str(dbt_project), "--output", str(output_dir)])
    assert (output_dir / "mdl.json").exists()
    assert (output_dir / "connection.json").exists()
    assert (output_dir / "schema_description.md").exists()


def test_cli_schema_description_non_empty(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main([str(dbt_project), "--output", str(output_dir)])
    content = (output_dir / "schema_description.md").read_text()
    assert len(content) > 0
    assert "customers" in content


def test_cli_mdl_json_valid(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main([str(dbt_project), "--output", str(output_dir)])
    data = json.loads((output_dir / "mdl.json").read_text())
    assert "models" in data
    assert "relationships" in data
    assert any(m["name"] == "customers" for m in data["models"])


def test_cli_connection_json_valid(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main([str(dbt_project), "--output", str(output_dir)])
    data = json.loads((output_dir / "connection.json").read_text())
    assert "dataSource" in data
    assert "connection" in data
    assert data["dataSource"] == "duckdb"


def test_cli_all_models_included_by_default(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main([str(dbt_project), "--output", str(output_dir)])
    data = json.loads((output_dir / "mdl.json").read_text())
    model_names = [m["name"] for m in data["models"]]
    assert "stg_orders" in model_names


def test_cli_exclude_single_pattern(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main([str(dbt_project), "--exclude", "^stg_", "--output", str(output_dir)])
    data = json.loads((output_dir / "mdl.json").read_text())
    model_names = [m["name"] for m in data["models"]]
    assert "stg_orders" not in model_names
    assert "customers" in model_names


def test_cli_exclude_multiple_patterns(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    main(
        [
            str(dbt_project),
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


def test_cli_custom_catalog_and_manifest(dbt_project, tmp_path):
    output_dir = tmp_path / "out"
    catalog = dbt_project / "target" / "catalog.json"
    manifest = dbt_project / "target" / "manifest.json"
    main(
        [
            str(dbt_project),
            "--catalog",
            str(catalog),
            "--manifest",
            str(manifest),
            "--output",
            str(output_dir),
        ]
    )
    assert (output_dir / "mdl.json").exists()


def test_cli_missing_project_exits_1(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main([str(tmp_path), "--output", str(tmp_path / "out")])
    assert exc_info.value.code == 1


def test_cli_default_output_is_cwd(dbt_project, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    main([str(dbt_project)])
    assert (tmp_path / "mdl.json").exists()
    assert (tmp_path / "connection.json").exists()
