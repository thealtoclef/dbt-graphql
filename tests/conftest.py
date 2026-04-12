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
