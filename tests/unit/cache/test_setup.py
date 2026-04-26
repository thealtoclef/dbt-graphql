"""Cache setup wiring.

Tests the lifespan-style hooks that bind cashews to the configured URL.
"""

from __future__ import annotations

import pytest
from cashews import cache

from dbt_graphql.cache.config import CacheConfig
from dbt_graphql.cache.setup import close_cache, is_configured, setup_cache


@pytest.mark.asyncio
async def test_default_config_boots_and_serves():
    setup_cache(CacheConfig())
    try:
        assert is_configured()
        await cache.set("k", 42, expire=60)
        assert await cache.get("k") == 42
    finally:
        await close_cache()
        assert not is_configured()


@pytest.mark.asyncio
async def test_disabled_skipped():
    setup_cache(CacheConfig(enabled=False))
    # ``enabled=False`` is the only operator-facing way to disable
    # caching from YAML; ``is_configured`` stays False so the resolver
    # never reaches into a half-initialized cashews backend.
    assert not is_configured()
    await close_cache()


@pytest.mark.asyncio
async def test_setup_idempotent_repeated_calls():
    """Calling setup_cache twice must not raise or double-register backends."""
    cfg = CacheConfig()
    setup_cache(cfg)
    setup_cache(cfg)  # no exception
    try:
        await cache.set("k", "v", expire=10)
        assert await cache.get("k") == "v"
    finally:
        await close_cache()


@pytest.mark.asyncio
async def test_close_without_setup_is_safe():
    """Lifespan teardown may run even if setup never did (e.g., setup raised)."""
    await close_cache()  # must not raise
