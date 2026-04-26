"""L1 parsed-doc cache (sync LRU used by Ariadne's ``query_parser``)."""

from __future__ import annotations

import pytest
from graphql import DocumentNode, GraphQLError, print_ast

from dbt_graphql.cache import stats
from dbt_graphql.cache.parsed_doc import configure_sync_lru, parse_sync_cached


@pytest.fixture(autouse=True)
def _reset_sync_lru():
    # The LRU is a module-global; reset it (and stats) between tests so
    # a hit recorded here came from work this test did, not a sibling.
    configure_sync_lru(1000)
    stats.reset()
    yield
    configure_sync_lru(1000)
    stats.reset()


def test_first_call_misses_returns_document():
    doc = parse_sync_cached("{ customers { id name } }")
    assert isinstance(doc, DocumentNode)
    assert stats.parsed_doc.miss == 1
    assert stats.parsed_doc.hit == 0


def test_repeat_call_hits():
    q = "{ customers { id name } }"
    a = parse_sync_cached(q)
    b = parse_sync_cached(q)
    # Sync LRU returns the same cached DocumentNode object on hit.
    assert a is b
    assert stats.parsed_doc.miss == 1
    assert stats.parsed_doc.hit == 1


def test_whitespace_variants_share_cache():
    a = parse_sync_cached("{ customers { id name } }")
    b = parse_sync_cached("{\n  customers   {\n    id\n    name\n  }\n}")
    assert print_ast(a) == print_ast(b)
    assert stats.parsed_doc.hit == 1
    assert stats.parsed_doc.miss == 1


def test_distinct_queries_independent():
    parse_sync_cached("{ customers { id } }")
    parse_sync_cached("{ customers { name } }")
    assert stats.parsed_doc.miss == 2
    assert stats.parsed_doc.hit == 0


def test_invalid_query_cached_as_exception():
    bad = "{ invalid {{{{"
    with pytest.raises(GraphQLError):
        parse_sync_cached(bad)
    # Second call must NOT re-parse — cached failure short-circuits.
    with pytest.raises(GraphQLError):
        parse_sync_cached(bad)
    assert stats.parsed_doc.miss == 1
    assert stats.parsed_doc.hit == 1


def test_lru_eviction_respects_max_size():
    configure_sync_lru(2)
    parse_sync_cached("{ a { id } }")
    parse_sync_cached("{ b { id } }")
    parse_sync_cached("{ c { id } }")  # evicts the a-entry
    stats.reset()
    parse_sync_cached("{ a { id } }")  # must re-parse
    parse_sync_cached("{ c { id } }")  # still cached
    assert stats.parsed_doc.miss == 1
    assert stats.parsed_doc.hit == 1
