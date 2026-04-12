from wren_dbt_converter.processors.tests_preprocessor import (
    preprocess_tests,
    _sanitize_enum_name,
)


def test_not_null_extraction(manifest):
    result = preprocess_tests(manifest)
    key = "model.jaffle_shop.customers.customer_id"
    assert result.column_to_not_null.get(key) is True


def test_accepted_values_enum_created(manifest):
    result = preprocess_tests(manifest)
    # orders.status / stg_orders.status → deduped to 1; stg_payments.payment_method → 1 more
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


def test_accepted_values_column_mapping(manifest):
    result = preprocess_tests(manifest)
    key = "model.jaffle_shop.orders.status"
    assert key in result.column_to_enum_name


def test_no_false_positives_for_non_test_nodes(manifest):
    result = preprocess_tests(manifest)
    # customers.first_name has no not_null test
    key = "model.jaffle_shop.customers.first_name"
    assert key not in result.column_to_not_null


def test_deduplication_same_value_set(manifest_path):
    """Two accepted_values tests with the same sorted values → single EnumDefinition."""
    from dbt_artifacts_parser.parser import parse_manifest
    import json

    data = json.loads(manifest_path.read_text())
    # Add a second accepted_values test on a different column, same values
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
            },  # same set, different order
            "namespace": None,
        },
    }
    m = parse_manifest(data)
    result = preprocess_tests(m)
    # 3 tests share same status values → 1 status enum; stg_payments.payment_method → 1 more = 2 total
    assert len(result.enum_definitions) == 2


def test_sanitize_enum_name():
    assert _sanitize_enum_name("status_enum") == "status_enum"
    assert _sanitize_enum_name("my-col_enum") == "mycol_enum"
    assert _sanitize_enum_name("123col_enum") == "_123col_enum"
    assert _sanitize_enum_name("") == "enum"
