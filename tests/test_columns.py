from wren import DataSource as WrenDataSource
from wren_dbt_converter.processors.columns import convert_columns


def test_columns_sorted_by_index(catalog, manifest):
    catalog_node = catalog.nodes["model.jaffle_shop.customers"]
    manifest_node = manifest.nodes["model.jaffle_shop.customers"]
    cols = convert_columns(
        catalog_node=catalog_node,
        manifest_node=manifest_node,
        data_source=WrenDataSource.duckdb,
        column_to_enum_name={},
        column_to_not_null={},
    )
    # Indexes in fixture: customer_id=1, first_name=2, last_name=3, first_order=4, most_recent_order=5, number_of_orders=6, customer_lifetime_value=7
    assert [c.name for c in cols] == [
        "customer_id",
        "first_name",
        "last_name",
        "first_order",
        "most_recent_order",
        "number_of_orders",
        "customer_lifetime_value",
    ]


def test_not_null_applied(catalog, manifest):
    catalog_node = catalog.nodes["model.jaffle_shop.customers"]
    manifest_node = manifest.nodes["model.jaffle_shop.customers"]
    not_null_map = {"model.jaffle_shop.customers.customer_id": True}
    cols = convert_columns(
        catalog_node=catalog_node,
        manifest_node=manifest_node,
        data_source=WrenDataSource.postgres,
        column_to_enum_name={},
        column_to_not_null=not_null_map,
    )
    by_name = {c.name: c for c in cols}
    assert by_name["customer_id"].not_null is True
    assert by_name["first_name"].not_null is False


def test_enum_in_properties(catalog, manifest):
    catalog_node = catalog.nodes["model.jaffle_shop.orders"]
    manifest_node = manifest.nodes["model.jaffle_shop.orders"]
    enum_map = {"model.jaffle_shop.orders.status": "status_enum"}
    cols = convert_columns(
        catalog_node=catalog_node,
        manifest_node=manifest_node,
        data_source=WrenDataSource.postgres,
        column_to_enum_name=enum_map,
        column_to_not_null={},
    )
    by_name = {c.name: c for c in cols}
    assert by_name["status"].properties is not None
    assert by_name["status"].properties["enumDefinition"] == "status_enum"


def test_description_from_manifest(catalog, manifest):
    catalog_node = catalog.nodes["model.jaffle_shop.customers"]
    manifest_node = manifest.nodes["model.jaffle_shop.customers"]
    cols = convert_columns(
        catalog_node=catalog_node,
        manifest_node=manifest_node,
        data_source=WrenDataSource.postgres,
        column_to_enum_name={},
        column_to_not_null={},
    )
    by_name = {c.name: c for c in cols}
    assert by_name["customer_id"].properties is not None
    assert (
        by_name["customer_id"].properties["description"]
        == "This is a unique identifier for a customer"
    )


def test_comment_absent_when_null(catalog, manifest):
    catalog_node = catalog.nodes["model.jaffle_shop.customers"]
    manifest_node = manifest.nodes["model.jaffle_shop.customers"]
    cols = convert_columns(
        catalog_node=catalog_node,
        manifest_node=manifest_node,
        data_source=WrenDataSource.postgres,
        column_to_enum_name={},
        column_to_not_null={},
    )
    by_name = {c.name: c for c in cols}
    # Real jaffle_shop fixtures have no catalog comments — "comment" key must be absent
    for col in by_name.values():
        assert col.properties is None or "comment" not in col.properties


def test_type_mapped_via_data_source(catalog, manifest):
    catalog_node = catalog.nodes["model.jaffle_shop.customers"]
    manifest_node = manifest.nodes["model.jaffle_shop.customers"]
    cols = convert_columns(
        catalog_node=catalog_node,
        manifest_node=manifest_node,
        data_source=WrenDataSource.duckdb,
        column_to_enum_name={},
        column_to_not_null={},
    )
    by_name = {c.name: c for c in cols}
    # Fixture has INTEGER → duckdb maps to "integer"
    assert by_name["customer_id"].type == "integer"
    # VARCHAR → "varchar"
    assert by_name["first_name"].type == "varchar"
