import json
import shutil
import pytest
from pathlib import Path

from wren_dbt_converter import build_manifest, ConvertResult
from wren_dbt_converter.models.wren_mdl import WrenMDLManifest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def dbt_project(tmp_path):
    """Create a minimal dbt project layout pointing to fixture artifacts."""
    # Copy fixtures into a project with expected layout
    (tmp_path / "target").mkdir()
    shutil.copy(FIXTURES_DIR / "catalog.json", tmp_path / "target" / "catalog.json")
    shutil.copy(FIXTURES_DIR / "manifest.json", tmp_path / "target" / "manifest.json")
    shutil.copy(FIXTURES_DIR / "profiles.yml", tmp_path / "profiles.yml")
    shutil.copy(FIXTURES_DIR / "dbt_project.yml", tmp_path / "dbt_project.yml")
    return tmp_path


def test_build_manifest_returns_result(dbt_project):
    result = build_manifest(dbt_project)
    assert isinstance(result, ConvertResult)
    assert isinstance(result.manifest, WrenMDLManifest)


def test_build_manifest_has_models(dbt_project):
    result = build_manifest(dbt_project)
    model_names = {m.name for m in result.manifest.models}
    assert "customers" in model_names
    assert "orders" in model_names
    # all models included by default
    assert "stg_orders" in model_names


def test_build_manifest_exclude_pattern(dbt_project):
    result = build_manifest(dbt_project, exclude_pattern=r"^(stg_|staging_)")
    model_names = {m.name for m in result.manifest.models}
    assert "customers" in model_names
    assert "orders" in model_names
    assert "stg_orders" not in model_names


def test_build_manifest_has_relationship(dbt_project):
    result = build_manifest(dbt_project)
    assert len(result.manifest.relationships) == 1
    rel = result.manifest.relationships[0]
    assert set(rel.models) == {"orders", "customers"}


def test_build_manifest_has_enum(dbt_project):
    result = build_manifest(dbt_project)
    # orders.status / stg_orders.status → 1 deduped enum; stg_payments.payment_method → 1 more
    assert len(result.manifest.enum_definitions) == 2
    all_value_sets = {
        tuple(sorted(v.name for v in e.values))
        for e in result.manifest.enum_definitions
    }
    assert (
        "completed",
        "placed",
        "return_pending",
        "returned",
        "shipped",
    ) in all_value_sets
    assert ("bank_transfer", "coupon", "credit_card", "gift_card") in all_value_sets


def test_manifest_str_is_base64_json(dbt_project):
    import base64

    result = build_manifest(dbt_project)
    raw = base64.b64decode(result.manifest_str)
    data = json.loads(raw)
    assert "models" in data
    assert "relationships" in data


def test_missing_dbt_project_yml(tmp_path):
    with pytest.raises(FileNotFoundError, match="dbt_project.yml"):
        build_manifest(tmp_path)


def test_missing_catalog(tmp_path):
    (tmp_path / "dbt_project.yml").touch()
    (tmp_path / "target").mkdir()
    shutil.copy(FIXTURES_DIR / "profiles.yml", tmp_path / "profiles.yml")
    with pytest.raises(FileNotFoundError, match="catalog.json"):
        build_manifest(tmp_path)


def test_missing_profiles(tmp_path):
    (tmp_path / "dbt_project.yml").touch()
    (tmp_path / "target").mkdir()
    shutil.copy(FIXTURES_DIR / "catalog.json", tmp_path / "target" / "catalog.json")
    shutil.copy(FIXTURES_DIR / "manifest.json", tmp_path / "target" / "manifest.json")
    with pytest.raises(FileNotFoundError, match="profiles.yml"):
        build_manifest(tmp_path)


def test_not_null_propagated(dbt_project):
    result = build_manifest(dbt_project)
    customers = next(m for m in result.manifest.models if m.name == "customers")
    by_name = {c.name: c for c in customers.columns}
    assert by_name["customer_id"].not_null is True


def test_model_description_in_properties(dbt_project):
    result = build_manifest(dbt_project)
    customers = next(m for m in result.manifest.models if m.name == "customers")
    assert customers.properties is not None
    assert (
        customers.properties.get("description")
        == "This table has basic information about a customer, as well as some derived facts based on a customer's orders"
    )
