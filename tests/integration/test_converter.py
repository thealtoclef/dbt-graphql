import json
import base64
import shutil
import pytest
from pathlib import Path

from dbt_mdl import extract_project, format_mdl, ConvertResult
from dbt_mdl.wren.models import WrenMDLManifest


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


def _build(**kwargs):
    project = extract_project(CATALOG, MANIFEST, **kwargs)
    return format_mdl(project)


def test_build_manifest_returns_result():
    result = _build()
    assert isinstance(result, ConvertResult)
    assert isinstance(result.manifest, WrenMDLManifest)


def test_build_manifest_has_models():
    result = _build()
    model_names = {m.name for m in result.manifest.models}
    assert "customers" in model_names
    assert "orders" in model_names
    assert "stg_orders" in model_names


def test_build_manifest_exclude_patterns():
    result = _build(exclude_patterns=[r"^stg_", r"^staging_"])
    model_names = {m.name for m in result.manifest.models}
    assert "customers" in model_names
    assert "orders" in model_names
    assert "stg_orders" not in model_names


def test_build_manifest_exclude_multiple_independent_patterns():
    result = _build(exclude_patterns=[r"^cust", r"^ord"])
    model_names = {m.name for m in result.manifest.models}
    assert "customers" not in model_names
    assert "orders" not in model_names
    assert "stg_orders" in model_names


def test_build_manifest_has_relationship():
    result = _build()
    assert len(result.manifest.relationships) == 1
    rel = result.manifest.relationships[0]
    model_names = set(rel.models)
    assert model_names == {"orders", "customers"}


def test_build_manifest_has_enum():
    result = _build()
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


def test_manifest_str_is_base64_json():
    result = _build()
    raw = base64.b64decode(result.manifest_str)
    data = json.loads(raw)
    assert "models" in data
    assert "relationships" in data


def test_missing_catalog(tmp_path):
    manifest = tmp_path / "manifest.json"
    shutil.copy(MANIFEST, manifest)
    with pytest.raises(FileNotFoundError, match="catalog.json"):
        extract_project(
            catalog_path=tmp_path / "catalog.json",
            manifest_path=manifest,
        )


def test_missing_manifest(tmp_path):
    catalog = tmp_path / "catalog.json"
    shutil.copy(CATALOG, catalog)
    with pytest.raises(FileNotFoundError, match="manifest.json"):
        extract_project(
            catalog_path=catalog,
            manifest_path=tmp_path / "manifest.json",
        )


def test_not_null_propagated():
    result = _build()
    customers = next(m for m in result.manifest.models if m.name == "customers")
    by_name = {c.name: c for c in customers.columns}
    assert by_name["customer_id"].not_null is True


def test_model_description_in_properties():
    result = _build()
    customers = next(m for m in result.manifest.models if m.name == "customers")
    assert customers.properties is not None
    assert (
        customers.properties.get("description")
        == "This table has basic information about a customer, as well as some derived facts based on a customer's orders"
    )
