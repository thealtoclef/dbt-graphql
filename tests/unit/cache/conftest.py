"""Per-test cache isolation.

cashews exposes a process-global ``cache`` singleton. Without explicit
teardown, state from one test leaks into the next — and because pytest-asyncio
runs tests in a single event loop by default, a stale entry from test A can
satisfy a lookup in test B and silently turn a real miss into a phantom hit.

The fixture below configures a fresh in-memory backend at the start of every
test and clears state afterwards. We deliberately do NOT use ``autouse`` — a
few tests need to inspect setup behavior themselves.
"""

from __future__ import annotations

import pytest_asyncio
from cashews import cache

from dbt_graphql.cache.setup import close_cache, setup_cache
from dbt_graphql.cache.config import CacheBackendConfig, CacheConfig
from dbt_graphql.cache.stats import stats


@pytest_asyncio.fixture
async def fresh_cache():
    """In-memory cashews + zeroed stats. Yields the active CacheConfig."""
    cfg = CacheConfig(
        backends=[CacheBackendConfig(url="mem://?size=1000")],
    )
    setup_cache(cfg)
    await cache.clear()
    stats.reset()
    yield cfg
    await cache.clear()
    await close_cache()
    stats.reset()
