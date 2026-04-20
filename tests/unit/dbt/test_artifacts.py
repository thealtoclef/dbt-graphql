"""Tests for dbt artifact loading (load_catalog, load_manifest)."""

import json
from pathlib import Path

import pytest

from dbt_graphql.dbt.artifacts import load_catalog, load_manifest

FIXTURES = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES / "catalog.json"
MANIFEST = FIXTURES / "manifest.json"


class TestLoadCatalog:
    def test_returns_nodes(self):
        catalog = load_catalog(CATALOG)
        assert hasattr(catalog, "nodes")
        assert len(catalog.nodes) > 0

    def test_model_nodes_have_metadata(self):
        catalog = load_catalog(CATALOG)
        model_nodes = {k: v for k, v in catalog.nodes.items() if k.startswith("model.")}
        assert len(model_nodes) > 0
        for node in model_nodes.values():
            assert node.metadata.name is not None

    def test_model_nodes_have_columns(self):
        catalog = load_catalog(CATALOG)
        model_nodes = {k: v for k, v in catalog.nodes.items() if k.startswith("model.")}
        for node in model_nodes.values():
            assert node.columns is not None
            assert len(node.columns) > 0

    def test_column_has_type(self):
        catalog = load_catalog(CATALOG)
        for key, node in catalog.nodes.items():
            if not key.startswith("model."):
                continue
            for col in node.columns.values():
                assert col.type is not None
            break

    def test_missing_file_raises(self):
        with pytest.raises(Exception):
            load_catalog("/no/such/catalog.json")


class TestLoadManifest:
    def test_returns_nodes(self):
        manifest = load_manifest(MANIFEST)
        assert hasattr(manifest, "nodes")
        assert len(manifest.nodes) > 0

    def test_has_metadata(self):
        manifest = load_manifest(MANIFEST)
        assert hasattr(manifest, "metadata")

    def test_model_nodes_present(self):
        manifest = load_manifest(MANIFEST)
        model_keys = [k for k in manifest.nodes if k.startswith("model.")]
        assert len(model_keys) > 0

    def test_test_nodes_present(self):
        manifest = load_manifest(MANIFEST)
        test_keys = [k for k in manifest.nodes if k.startswith("test.")]
        assert len(test_keys) > 0

    def test_missing_file_raises(self):
        with pytest.raises(Exception):
            load_manifest("/no/such/manifest.json")

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")
        with pytest.raises(Exception):
            load_manifest(bad)

    def test_wrong_schema_raises(self, tmp_path):
        bad = tmp_path / "manifest.json"
        bad.write_text(json.dumps({"totally": "wrong"}))
        with pytest.raises(Exception):
            load_manifest(bad)
