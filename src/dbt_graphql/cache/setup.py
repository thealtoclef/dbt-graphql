"""Lifespan hooks that bind cashews to the configured backend URL.

Tests rely on ``setup_cache`` being idempotent: calling it twice with the
same config in a single process must not raise. cashews itself does not
guard against double-setup, so we tear down before re-setting up.
"""

from __future__ import annotations

from cashews import cache
from loguru import logger

from .config import CacheConfig

_CONFIGURED: bool = False


def setup_cache(cfg: CacheConfig) -> None:
    """Bind cashews to ``cfg.url``. Idempotent. No-op when ``enabled=False``."""
    global _CONFIGURED
    if _CONFIGURED:
        cache._backends.clear()  # type: ignore[attr-defined]
        _CONFIGURED = False

    if not cfg.enabled:
        logger.info("cache: disabled (cfg.enabled=False)")
        return

    cache.setup(cfg.url)
    logger.info("cache backend: {}", cfg.url)
    _CONFIGURED = True


async def close_cache() -> None:
    """Release cashews resources. Safe to call from a Starlette lifespan
    even if setup never ran."""
    global _CONFIGURED
    if not _CONFIGURED:
        return
    await cache.close()
    _CONFIGURED = False


def is_configured() -> bool:
    return _CONFIGURED
