import json
from pathlib import Path

from dbt_graphql.dbt.artifacts import load_manifest
from dbt_graphql.dbt.processors.data_tests import build_relationships
from dbt_graphql.ir.models import Cardinality, ProcessorRelationship


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
MANIFEST = FIXTURES_DIR / "manifest.json"


def test_builds_relationship_from_test():
    manifest = load_manifest(MANIFEST)
    rels = build_relationships(manifest)
    assert len(rels) == 1
    rel = rels[0]
    assert isinstance(rel, ProcessorRelationship)
    assert "orders" in rel.name
    assert "customers" in rel.name
    assert rel.cardinality == Cardinality.many_to_one
    assert rel.from_columns == ["customer_id"]
    assert rel.to_columns == ["customer_id"]
    assert set(rel.models) == {"orders", "customers"}


def test_deduplication(tmp_path):
    from dbt_artifacts_parser.parser import parse_manifest

    data = json.loads(MANIFEST.read_text())
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
    assert len(rels) == 1


def test_missing_refs_skipped(tmp_path):
    from dbt_artifacts_parser.parser import parse_manifest

    data = json.loads(MANIFEST.read_text())
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
    assert len(rels) == 1


def test_non_relationship_tests_ignored():
    manifest = load_manifest(MANIFEST)
    rels = build_relationships(manifest)
    for rel in rels:
        assert "not_null" not in rel.name
        assert "accepted_values" not in rel.name


def test_cardinality_is_many_to_one():
    manifest = load_manifest(MANIFEST)
    rels = build_relationships(manifest)
    assert all(r.cardinality == Cardinality.many_to_one for r in rels)


def test_quoted_column_names_stripped(tmp_path):
    from dbt_artifacts_parser.parser import parse_manifest

    data = json.loads(MANIFEST.read_text())
    test_key = "test.jaffle_shop.relationships_orders_customer_id__customer_id__ref_customers_.c6ec7f58f2"
    data["nodes"][test_key]["column_name"] = '"customer_id"'
    data["nodes"][test_key]["test_metadata"]["kwargs"]["field"] = '"customer_id"'

    m = parse_manifest(data)
    rels = build_relationships(m)
    assert len(rels) == 1
    assert rels[0].from_columns == ["customer_id"]
    assert rels[0].to_columns == ["customer_id"]
