"""L1 — parsed-doc cache.

Skips ``parse`` on repeated query strings. Wired into the GraphQL app via
Ariadne's ``query_parser`` hook (see ``api/app.py``).

Ariadne's ``query_parser`` hook is synchronous (``(context, data) -> doc``)
so we can't ``await`` cashews from there — the cache is a process-local
sync LRU. The plan's §3.2 also calls out that L1 is "intentionally always
in-memory".

Both successful parses and syntax errors are cached: re-validating an
invalid query would burn CPU on every retry from the same misbehaving
client. ``GraphQLSyntaxError`` is not safe to re-raise from cache (its
``__init__`` requires positional args we don't keep around) so we cache
the message and synthesize a generic ``GraphQLError`` on retrieval.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from graphql import DocumentNode, GraphQLError, parse as gql_parse

from .keys import parsed_doc_key
from .stats import stats


class _SyncLRU:
    """Tiny bounded LRU. Stores either a ``DocumentNode`` or a parse-error
    message string keyed by ``parsed_doc_key(query)``."""

    def __init__(self, max_size: int) -> None:
        self._max = max_size
        self._d: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any:
        try:
            value = self._d.pop(key)
        except KeyError:
            return None
        self._d[key] = value
        return value

    def set(self, key: str, value: Any) -> None:
        if key in self._d:
            self._d.pop(key)
        self._d[key] = value
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()


_sync_lru: _SyncLRU = _SyncLRU(max_size=1000)


def configure_sync_lru(max_size: int) -> None:
    """Resize the sync L1 cache. Wired from app startup. Also clears state
    so a fresh ``create_app`` does not inherit cache entries from the
    previous one (matters for tests)."""
    global _sync_lru
    _sync_lru = _SyncLRU(max_size=max_size)


_PARSE_ERR_TAG = "__parse_error__"


def parse_sync_cached(query: str) -> DocumentNode:
    """Sync L1 entry point used by Ariadne's ``query_parser`` hook."""
    key = parsed_doc_key(query)
    cached = _sync_lru.get(key)
    if cached is not None:
        stats.parsed_doc.hit += 1
        if isinstance(cached, tuple) and cached and cached[0] == _PARSE_ERR_TAG:
            raise GraphQLError(cached[1])
        return cached

    stats.parsed_doc.miss += 1
    try:
        doc = gql_parse(query)
    except GraphQLError as exc:
        _sync_lru.set(key, (_PARSE_ERR_TAG, str(exc)))
        raise
    _sync_lru.set(key, doc)
    return doc
