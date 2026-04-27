"""Key resolver tests: JWKS rotation/cold-start, static sources (env/file/url)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from joserfc.jwk import KeySet, OctKey, RSAKey

from dbt_graphql.graphql.auth.keys import (
    JWKSResolver,
    StaticKeyResolver,
    _parse_key_material,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://idp.test")


# ---------------------------------------------------------------------------
# StaticKeyResolver.from_env / from_file / from_url
# ---------------------------------------------------------------------------


def test_from_env_hmac(monkeypatch):
    secret = "x" * 32
    monkeypatch.setenv("MY_JWT_SECRET", secret)
    r = StaticKeyResolver.from_env("MY_JWT_SECRET")
    ks = _run(r.get())
    assert isinstance(ks, KeySet) and len(list(ks)) == 1


def test_from_env_missing_raises(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(ValueError, match="not set"):
        StaticKeyResolver.from_env("MISSING")


def test_from_env_empty_raises(monkeypatch):
    monkeypatch.setenv("EMPTY", "")
    with pytest.raises(ValueError, match="not set"):
        StaticKeyResolver.from_env("EMPTY")


def test_from_file_pem(tmp_path):
    rsa = RSAKey.generate_key(2048)
    p = tmp_path / "key.pem"
    p.write_bytes(rsa.as_pem())
    r = StaticKeyResolver.from_file(p)
    ks = _run(r.get())
    assert len(list(ks)) == 1


def test_from_file_jwk_json(tmp_path):
    rsa = RSAKey.generate_key(2048)
    p = tmp_path / "key.jwk"
    p.write_bytes(json.dumps(rsa.as_dict(private=False)).encode())
    r = StaticKeyResolver.from_file(p)
    ks = _run(r.get())
    assert len(list(ks)) == 1


def test_from_url_pem_via_octet_stream():
    rsa = RSAKey.generate_key(2048)
    pem = rsa.as_pem()

    def handler(_req):
        return httpx.Response(
            200, content=pem, headers={"content-type": "application/x-pem-file"}
        )

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = await StaticKeyResolver.from_url("http://idp.test/key.pem", http)
            assert len(list(await r.get())) == 1

    _run(run())


def test_from_url_single_jwk_json():
    rsa = RSAKey.generate_key(2048)

    def handler(_req):
        return httpx.Response(200, json=rsa.as_dict(private=False))

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = await StaticKeyResolver.from_url("http://idp.test/key", http)
            assert len(list(await r.get())) == 1

    _run(run())


def test_from_url_jwks_set_via_keys_field():
    """A URL returning {"keys": [...]} is treated as a full JWKS."""
    rsa1 = RSAKey.generate_key(2048)
    rsa2 = RSAKey.generate_key(2048)

    def handler(_req):
        return httpx.Response(
            200,
            json={"keys": [rsa1.as_dict(private=False), rsa2.as_dict(private=False)]},
        )

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = await StaticKeyResolver.from_url("http://idp.test/jwks", http)
            assert len(list(await r.get())) == 2

    _run(run())


def test_from_url_http_error_propagates():
    def handler(_req):
        return httpx.Response(503)

    async def run():
        async with _client(_mock_transport(handler)) as http:
            with pytest.raises(httpx.HTTPStatusError):
                await StaticKeyResolver.from_url("http://idp.test/key", http)

    _run(run())


# ---------------------------------------------------------------------------
# JWKSResolver
# ---------------------------------------------------------------------------


def _jwks_payload(*keys: RSAKey) -> dict:
    return {"keys": [k.as_dict(private=False) for k in keys]}


def test_jwks_fetch_and_cache_within_ttl():
    rsa = RSAKey.generate_key(2048)
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(200, json=_jwks_payload(rsa))

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = JWKSResolver("http://idp.test/jwks", cache_ttl=60, http=http)
            ks1 = await r.get()
            ks2 = await r.get()
            assert ks1 is ks2
            assert calls["n"] == 1

    _run(run())


def test_jwks_kid_miss_inside_ttl_does_not_refetch():
    """A token with an unknown kid must NOT trigger a JWKS refetch while
    the cache is still warm — that's a thundering-herd vector. Verifier
    fails the request; resolver does not re-call the IdP."""
    rsa = RSAKey.generate_key(2048, parameters={"kid": "A"})
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(200, json=_jwks_payload(rsa))

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = JWKSResolver("http://idp.test/jwks", cache_ttl=60, http=http)
            ks_first = await r.get()
            # Caller would now try to verify a token with kid=B against this
            # keyset, which fails. They call .get() again — no refetch.
            ks_second = await r.get()
            assert ks_first is ks_second
            assert calls["n"] == 1

    _run(run())


def test_jwks_no_last_good_fallback_after_ttl():
    """Once TTL expires and refetch fails, raise — no stale-cache fallback."""
    rsa = RSAKey.generate_key(2048)
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_jwks_payload(rsa))
        return httpx.Response(503)

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = JWKSResolver("http://idp.test/jwks", cache_ttl=0, http=http)
            await r.get()
            with pytest.raises(httpx.HTTPStatusError):
                await r.get()

    _run(run())


def test_jwks_cold_start_outage_raises():
    def handler(_req):
        return httpx.Response(503)

    async def run():
        async with _client(_mock_transport(handler)) as http:
            r = JWKSResolver("http://idp.test/jwks", cache_ttl=60, http=http)
            with pytest.raises(httpx.HTTPStatusError):
                await r.get()

    _run(run())


def test_jwks_concurrent_fetches_coalesce():
    """Real concurrency: handler awaits a sleep so that without the lock,
    multiple gather()ed callers would race in."""
    rsa = RSAKey.generate_key(2048)
    calls = {"n": 0}

    async def slow_handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_jwks_payload(rsa))

    async def run():
        transport = httpx.MockTransport(slow_handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://idp.test"
        ) as http:
            r = JWKSResolver("http://idp.test/jwks", cache_ttl=60, http=http)
            results = await asyncio.gather(*(r.get() for _ in range(5)))
            assert all(x is results[0] for x in results)
            assert calls["n"] == 1

    _run(run())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def test_parse_key_material_jwk_dict_via_bytes():
    rsa = RSAKey.generate_key(2048)
    jwk_bytes = json.dumps(rsa.as_dict(private=False)).encode()
    ks = _parse_key_material(jwk_bytes)
    assert len(list(ks)) == 1


def test_parse_key_material_hmac_secret():
    ks = _parse_key_material("x" * 32)
    keys = list(ks)
    assert len(keys) == 1 and isinstance(keys[0], OctKey)
