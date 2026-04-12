from wren_dbt_converter.processors.relationships import build_relationships
from wren_dbt_converter.models.wren_mdl import Relationship


def test_builds_relationship_from_test(manifest):
    rels = build_relationships(manifest)
    assert len(rels) == 1
    rel = rels[0]
    assert isinstance(rel, Relationship)
    assert "orders" in rel.name
    assert "customers" in rel.name
    assert rel.join_type == "MANY_TO_ONE"
    assert '"orders"."customer_id" = "customers"."customer_id"' == rel.condition
    assert set(rel.models) == {"orders", "customers"}


def test_deduplication(manifest_path):
    """Identical relationship test appearing twice → deduplicated to one."""
    from dbt_artifacts_parser.parser import parse_manifest
    import json

    data = json.loads(manifest_path.read_text())
    # Duplicate the relationship test node
    dup = dict(
        data["nodes"][
            "test.jaffle_shop.relationships_orders_customer_id__customer_id__ref_customers_.c6ec7f58f2"
        ]
    )
    dup["unique_id"] = "test.jaffle_shop.relationships_orders_customer_id.dup"
    dup["name"] = "dup"
    dup["alias"] = "dup"
    data["nodes"][dup["unique_id"]] = dup

    m = parse_manifest(data)
    rels = build_relationships(m)
    # Still only 1 unique relationship
    assert len(rels) == 1


def test_missing_refs_skipped(manifest_path):
    """Test node with no refs → not added."""
    from dbt_artifacts_parser.parser import parse_manifest
    import json

    data = json.loads(manifest_path.read_text())
    data["nodes"]["test.jaffle_shop.relationships_no_refs.x"] = {
        "resource_type": "test",
        "database": "dev",
        "schema": "main",
        "name": "relationships_no_refs",
        "unique_id": "test.jaffle_shop.relationships_no_refs.x",
        "package_name": "jaffle_shop",
        "path": "x.sql",
        "original_file_path": "x.yml",
        "fqn": ["jaffle_shop", "relationships_no_refs"],
        "alias": "x",
        "checksum": {"name": "sha256", "checksum": "x"},
        "column_name": "col",
        "attached_node": "model.jaffle_shop.orders",
        "refs": [],
        "test_metadata": {
            "name": "relationships",
            "kwargs": {"field": "id"},
            "namespace": None,
        },
    }
    m = parse_manifest(data)
    rels = build_relationships(m)
    assert len(rels) == 1  # only the original


def test_non_relationship_tests_ignored(manifest):
    """not_null and accepted_values tests are not turned into relationships."""
    rels = build_relationships(manifest)
    for rel in rels:
        assert "not_null" not in rel.name
        assert "accepted_values" not in rel.name
