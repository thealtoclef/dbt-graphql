"""JWT key resolvers — JWKS (rotating) and static (PEM/JWK/HMAC)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Protocol

import httpx
from joserfc.jwk import ECKey, Key, KeySet, OctKey, OKPKey, RSAKey, import_key


class KeyResolver(Protocol):
    async def get(self) -> KeySet: ...


def _to_keyset(key_or_keyset: Key | KeySet) -> KeySet:
    if isinstance(key_or_keyset, KeySet):
        return key_or_keyset
    assert isinstance(key_or_keyset, (OctKey, RSAKey, ECKey, OKPKey))
    return KeySet([key_or_keyset])


def _parse_key_material(data: bytes | str) -> KeySet:
    """Parse PEM, JWK JSON, or raw HMAC bytes into a KeySet."""
    return _to_keyset(import_key(data))


class JWKSResolver:
    """Async JWKS fetcher: TTL-memoised keyset, lock-coalesced refetch."""

    def __init__(self, url: str, cache_ttl: int, http: httpx.AsyncClient) -> None:
        self._url = url
        self._http = http
        self._ttl = cache_ttl
        self._keyset: KeySet | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> KeySet:
        if self._keyset is not None and time.monotonic() < self._expires_at:
            return self._keyset
        async with self._lock:
            if self._keyset is not None and time.monotonic() < self._expires_at:
                return self._keyset
            resp = await self._http.get(self._url, timeout=5.0)
            resp.raise_for_status()
            self._keyset = KeySet.import_key_set(resp.json())
            self._expires_at = time.monotonic() + self._ttl
            return self._keyset


class StaticKeyResolver:
    """Single keyset, fetched once, cached forever."""

    def __init__(self, keyset: KeySet) -> None:
        self._keyset = keyset

    async def get(self) -> KeySet:
        return self._keyset

    @classmethod
    async def from_url(cls, url: str, http: httpx.AsyncClient) -> "StaticKeyResolver":
        resp = await http.get(url, timeout=5.0)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype in ("application/json", "application/jwk+json"):
            data = resp.json()
            if isinstance(data, dict) and "keys" in data:
                return cls(KeySet.import_key_set(data))
            return cls(_to_keyset(import_key(data)))
        return cls(_parse_key_material(resp.content))

    @classmethod
    def from_env(cls, var: str) -> "StaticKeyResolver":
        raw = os.environ.get(var)
        if raw is None or raw == "":
            raise ValueError(f"env var {var!r} not set or empty")
        return cls(_parse_key_material(raw))

    @classmethod
    def from_file(cls, path: Path) -> "StaticKeyResolver":
        return cls(_parse_key_material(path.read_bytes()))
