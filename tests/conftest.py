import shutil
from pathlib import Path
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def catalog_path(fixtures_dir) -> Path:
    return fixtures_dir / "catalog.json"


@pytest.fixture
def manifest_path(fixtures_dir) -> Path:
    return fixtures_dir / "manifest.json"


@pytest.fixture
def profiles_path(fixtures_dir) -> Path:
    return fixtures_dir / "profiles.yml"


@pytest.fixture
def catalog(catalog_path):
    from wren_dbt_converter.parsers.artifacts import load_catalog

    return load_catalog(catalog_path)


@pytest.fixture
def manifest(manifest_path):
    from wren_dbt_converter.parsers.artifacts import load_manifest

    return load_manifest(manifest_path)


@pytest.fixture
def profiles(profiles_path):
    from wren_dbt_converter.parsers.profiles_parser import analyze_dbt_profiles

    return analyze_dbt_profiles(profiles_path)


@pytest.fixture
def dbt_project(tmp_path):
    """Create a minimal dbt project layout pointing to fixture artifacts."""
    (tmp_path / "target").mkdir()
    shutil.copy(FIXTURES_DIR / "catalog.json", tmp_path / "target" / "catalog.json")
    shutil.copy(FIXTURES_DIR / "manifest.json", tmp_path / "target" / "manifest.json")
    shutil.copy(FIXTURES_DIR / "profiles.yml", tmp_path / "profiles.yml")
    shutil.copy(FIXTURES_DIR / "dbt_project.yml", tmp_path / "dbt_project.yml")
    return tmp_path
