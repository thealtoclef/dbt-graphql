"""End-to-end Starlette + AuthenticationMiddleware tests.

These do not need a database — they wire the auth middleware around a
trivial route and verify the full RFC 6750 path: 401 + WWW-Authenticate
on bad tokens, anonymous on missing tokens, success on valid tokens.
Also exercises the JWKS rotation case via httpx.MockTransport so the
JWKSResolver behavior is reachable from a real request flow.
"""

from __future__ import annotations

import time

import httpx
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import OctKey, RSAKey
from starlette.applications import Starlette
from starlette.authentication import requires
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from dbt_graphql.graphql.auth import auth_on_error, build_auth_backend
from dbt_graphql.graphql.auth.backend import JWTAuthBackend
from dbt_graphql.graphql.auth.keys import JWKSResolver
from dbt_graphql.graphql.auth.verifier import Verifier
from dbt_graphql.config import JWTConfig


async def _whoami(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "authenticated": request.user.is_authenticated,
            "sub": getattr(request.user.payload, "sub", None),
            "scopes": list(request.auth.scopes),
        }
    )


@requires("authenticated")
async def _protected(request: Request) -> JSONResponse:
    return JSONResponse({"sub": request.user.payload.sub})


def _app(backend: JWTAuthBackend) -> Starlette:
    return Starlette(
        routes=[
            Route("/me", _whoami),
            Route("/protected", _protected),
        ],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=backend,
                on_error=auth_on_error,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


def test_disabled_treats_every_request_as_anonymous():
    backend, _ = build_auth_backend(JWTConfig(enabled=False))
    with TestClient(_app(backend)) as c:
        # Even with a token, payload is not read.
        resp = c.get("/me", headers={"Authorization": "Bearer bogus"})
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": False, "sub": None, "scopes": []}


# ---------------------------------------------------------------------------
# HMAC happy path + 401 paths
# ---------------------------------------------------------------------------


def test_valid_token_authenticates(monkeypatch):
    secret = "x" * 32
    monkeypatch.setenv("HMAC_SECRET", secret)
    backend, _ = build_auth_backend(
        JWTConfig(enabled=True, algorithms=["HS256"], key_env="HMAC_SECRET")
    )
    token = joserfc_jwt.encode(
        {"alg": "HS256"},
        {"sub": "alice", "exp": int(time.time()) + 60},
        OctKey.import_key(secret),
    )
    with TestClient(_app(backend)) as c:
        resp = c.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["sub"] == "alice"
        assert "authenticated" in body["scopes"]


def test_invalid_signature_returns_401_with_www_authenticate(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET", "a" * 32)
    backend, _ = build_auth_backend(
        JWTConfig(enabled=True, algorithms=["HS256"], key_env="HMAC_SECRET")
    )
    # Sign with a different secret → signature invalid.
    token = joserfc_jwt.encode(
        {"alg": "HS256"},
        {"sub": "u", "exp": int(time.time()) + 60},
        OctKey.import_key("b" * 32),
    )
    with TestClient(_app(backend)) as c:
        resp = c.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        www = resp.headers["WWW-Authenticate"]
        assert www.startswith("Bearer ")
        assert 'error="invalid_token"' in www


def test_missing_token_on_protected_route_is_403(monkeypatch):
    """No token = anonymous = `requires('authenticated')` returns 403."""
    monkeypatch.setenv("HMAC_SECRET", "a" * 32)
    backend, _ = build_auth_backend(
        JWTConfig(enabled=True, algorithms=["HS256"], key_env="HMAC_SECRET")
    )
    with TestClient(_app(backend)) as c:
        resp = c.get("/protected")
        # Starlette's `requires` returns 403 for unauthenticated; our
        # middleware only converts AuthenticationError (bad token) to 401.
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# JWKS rotation: kid A → kid A+B; token signed by B validates after refetch
# ---------------------------------------------------------------------------


def test_jwks_rotation_recovers_after_cache_expiry():
    key_a = RSAKey.generate_key(2048, parameters={"kid": "A"})
    key_b = RSAKey.generate_key(2048, parameters={"kid": "B"})

    state = {"include_b": False}

    def handler(_req: httpx.Request) -> httpx.Response:
        keys = [key_a, key_b] if state["include_b"] else [key_a]
        return httpx.Response(
            200,
            json={"keys": [k.as_dict(private=False) for k in keys]},
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://idp.test")

    # cache_ttl=0 → every request triggers a refetch, modeling rotation.
    resolver = JWKSResolver("http://idp.test/jwks", cache_ttl=0, http=http)
    verifier = Verifier(
        key_resolver=resolver,
        algorithms=["RS256"],
        audience=None,
        issuer=None,
        leeway=30,
        required_claims=[],
        roles_claim=None,
    )
    backend = JWTAuthBackend(verifier)

    token_b = joserfc_jwt.encode(
        {"alg": "RS256", "kid": "B"},
        {"sub": "alice"},
        key_b,
    )

    with TestClient(_app(backend)) as c:
        # B not in JWKS yet → 401
        resp = c.get("/protected", headers={"Authorization": f"Bearer {token_b}"})
        assert resp.status_code == 401

        # IdP starts publishing B; cache_ttl=0 forces refetch on next call.
        state["include_b"] = True
        resp = c.get("/protected", headers={"Authorization": f"Bearer {token_b}"})
        assert resp.status_code == 200
        assert resp.json() == {"sub": "alice"}
