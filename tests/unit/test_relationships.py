import json
from pathlib import Path

from dbt_mdl.dbt.artifacts import load_manifest
from dbt_mdl.dbt.processors.relationships import build_relationships
from dbt_mdl.wren.models import Relationship, JoinType


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
MANIFEST = FIXTURES_DIR / "manifest.json"


def test_builds_relationship_from_test():
    manifest = load_manifest(MANIFEST)
    rels = build_relationships(manifest)
    assert len(rels) == 1
    rel = rels[0]
    assert isinstance(rel, Relationship)
    assert "orders" in rel.name
    assert "customers" in rel.name
    assert rel.join_type == JoinType.many_to_one
    assert '"orders"."customer_id" = "customers"."customer_id"' == rel.condition
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
