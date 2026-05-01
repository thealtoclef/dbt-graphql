"""Verify the registry → SDL → parse → print_ast pipeline round-trips.

The ``_sdl`` GraphQL field and the MCP ``describe_table`` tool both
rely on graphql-core's ``print_ast`` to render the per-request pruned
``DocumentNode`` back to text. If ``print_ast`` ever drifts from the
hand-written ``_registry_to_sdl``, those callers would emit subtly
different SDL than ``--output`` mode does. Round-trip every fixture
through both renderers as a guard.
"""

from pathlib import Path

from graphql import parse, print_ast

from dbt_graphql.graphql.sdl.generator import build_registry, build_source_doc
from dbt_graphql.pipeline import extract_project


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


def test_build_source_doc_parses_clean():
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    doc = build_source_doc(registry)
    assert len(doc.definitions) == len(list(registry))


def test_print_ast_roundtrips_registry_sdl():
    """parse(registry_sdl) → print_ast → parse must yield equivalent SDL."""
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    doc = build_source_doc(registry)
    printed = print_ast(doc)
    re_parsed = parse(printed)
    assert print_ast(re_parsed) == printed


def test_descriptions_preserved_through_print_ast():
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    doc = build_source_doc(registry)
    out = print_ast(doc)
    # Pick a known description from the jaffle-shop fixture.
    assert "This table has basic information about a customer" in out
