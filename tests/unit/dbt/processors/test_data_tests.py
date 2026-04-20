import json
from pathlib import Path

from dbt_graphql.dbt.artifacts import load_manifest
from dbt_graphql.dbt.processors.data_tests import (
    preprocess_tests,
    _sanitize_enum_name,
)


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
MANIFEST = FIXTURES_DIR / "manifest.json"


def test_not_null_extraction():
    manifest = load_manifest(MANIFEST)
    result = preprocess_tests(manifest)
    key = "model.jaffle_shop.customers.customer_id"
    assert result.column_to_not_null.get(key) is True


def test_accepted_values_enum_created():
    manifest = load_manifest(MANIFEST)
    result = preprocess_tests(manifest)
    assert len(result.enum_definitions) == 2
    all_value_sets = {
        tuple(sorted(v.name for v in e.values)) for e in result.enum_definitions
    }
    assert (
        "completed",
        "placed",
        "return_pending",
        "returned",
        "shipped",
    ) in all_value_sets
    assert ("bank_transfer", "coupon", "credit_card", "gift_card") in all_value_sets


def test_accepted_values_column_mapping():
    manifest = load_manifest(MANIFEST)
    result = preprocess_tests(manifest)
    key = "model.jaffle_shop.orders.status"
    assert key in result.column_to_enum_name


def test_no_false_positives_for_non_test_nodes():
    manifest = load_manifest(MANIFEST)
    result = preprocess_tests(manifest)
    key = "model.jaffle_shop.customers.first_name"
    assert key not in result.column_to_not_null


def test_deduplication_same_value_set():
    from dbt_artifacts_parser.parser import parse_manifest

    data = json.loads(MANIFEST.read_text())
    data["nodes"]["test.jaffle_shop.accepted_values_orders_status.dup"] = {
        "resource_type": "test",
        "database": "dev",
        "schema": "main",
        "name": "accepted_values_orders_status_dup",
        "unique_id": "test.jaffle_shop.accepted_values_orders_status.dup",
        "package_name": "jaffle_shop",
        "path": "dup.sql",
        "original_file_path": "models/orders.yml",
        "fqn": ["jaffle_shop", "accepted_values_orders_status_dup"],
        "alias": "dup",
        "checksum": {"name": "sha256", "checksum": "dup"},
        "column_name": "status",
        "attached_node": "model.jaffle_shop.orders",
        "test_metadata": {
            "name": "accepted_values",
            "kwargs": {
                "values": [
                    "shipped",
                    "placed",
                    "completed",
                    "returned",
                    "return_pending",
                ]
            },
            "namespace": None,
        },
    }
    m = parse_manifest(data)
    result = preprocess_tests(m)
    assert len(result.enum_definitions) == 2


def test_sanitize_enum_name():
    assert _sanitize_enum_name("status_enum") == "status_enum"
    assert _sanitize_enum_name("my-col_enum") == "mycol_enum"
    assert _sanitize_enum_name("123col_enum") == "_123col_enum"
    assert _sanitize_enum_name("") == "enum"
