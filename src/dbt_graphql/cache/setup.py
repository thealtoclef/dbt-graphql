"""Lifespan hooks that bind cashews backends to the configured URIs.

The full module exports a single ``cache`` singleton from ``cashews``. Setting
it up multiple times against different prefixes routes by prefix; the catch-all
backend has ``prefix=""``.

Tests rely on ``setup_cache`` being idempotent: calling it twice with the same
config in a single process must not raise. cashews itself does not guard
against double-setup, so we tear down before re-setting up.
"""

from __future__ import annotations

from cashews import cache
from loguru import logger

from .config import CacheConfig

_CONFIGURED: bool = False


def setup_cache(cfg: CacheConfig) -> None:
    """Wire all enabled backends. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        # Re-setup: clear before reconfiguring so prefix routing reflects
        # the new config (cashews accumulates backends otherwise).
        cache._backends.clear()  # type: ignore[attr-defined]
        _CONFIGURED = False

    enabled_backends = [b for b in cfg.backends if b.enabled]
    if not enabled_backends:
        logger.warning("cache: no enabled backends configured — caching disabled")
        return

    for b in enabled_backends:
        cache.setup(b.url, prefix=b.prefix)
        logger.info("cache backend: {} (prefix={!r})", b.url, b.prefix)
    _CONFIGURED = True


async def close_cache() -> None:
    """Release all cashews resources. Safe to call from a Starlette lifespan."""
    global _CONFIGURED
    if not _CONFIGURED:
        return
    try:
        await cache.close()
    except Exception as exc:
        logger.warning("cache.close failed (ignored): {}", exc)
    _CONFIGURED = False


def is_configured() -> bool:
    return _CONFIGURED
