"""Starlette JWTAuthBackend behavior tests (verifier mocked)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from starlette.authentication import AuthenticationError

from dbt_graphql.graphql.auth import JWTAuthBackend, JWTPayload, JWTUser
from dbt_graphql.graphql.auth.backend import auth_on_error, build_auth_backend
from dbt_graphql.graphql.auth.verifier import AuthError
from dbt_graphql.config import JWTConfig


def _conn(headers: dict | None = None):
    m = MagicMock()
    m.headers = headers or {}
    return m


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Disabled (verifier=None) → always anonymous
# ---------------------------------------------------------------------------


def test_disabled_no_header_anonymous():
    creds, user = _run(JWTAuthBackend(None).authenticate(_conn()))
    assert not user.is_authenticated
    assert "authenticated" not in creds.scopes


def test_disabled_token_present_still_anonymous():
    """enabled=False: token must not be read at all."""
    creds, user = _run(
        JWTAuthBackend(None).authenticate(
            _conn({"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x"})
        )
    )
    assert not user.is_authenticated
    assert "authenticated" not in creds.scopes


# ---------------------------------------------------------------------------
# Enabled (verifier set) → delegate
# ---------------------------------------------------------------------------


class _FakeVerifier:
    def __init__(self, claims=None, error=None, roles_claim="scope"):
        self._claims = claims
        self._error = error
        self.roles_claim = roles_claim

    async def verify(self, _token: str) -> dict:
        if self._error is not None:
            raise self._error
        return self._claims or {}


def test_no_authorization_header_anonymous():
    backend = JWTAuthBackend(_FakeVerifier(claims={"sub": "u"}))
    creds, user = _run(backend.authenticate(_conn()))
    assert not user.is_authenticated
    assert creds.scopes == []


def test_non_bearer_scheme_anonymous():
    backend = JWTAuthBackend(_FakeVerifier(claims={"sub": "u"}))
    _, user = _run(backend.authenticate(_conn({"Authorization": "Basic dXNlcjpwYXNz"})))
    assert not user.is_authenticated


def test_bearer_scheme_case_insensitive():
    """RFC 6750: Bearer scheme is case-insensitive."""
    backend = JWTAuthBackend(_FakeVerifier(claims={"sub": "u"}))
    creds, user = _run(backend.authenticate(_conn({"Authorization": "bearer token"})))
    assert user.is_authenticated
    assert "authenticated" in creds.scopes


def test_valid_token_yields_authenticated_user():
    backend = JWTAuthBackend(
        _FakeVerifier(claims={"sub": "u1", "claims": {"org": 42}, "scope": "read"})
    )
    creds, user = _run(backend.authenticate(_conn({"Authorization": "Bearer t"})))
    assert user.is_authenticated
    assert user.payload.sub == "u1"
    assert user.payload.claims.org == 42
    assert "authenticated" in creds.scopes
    assert "read" in creds.scopes


def test_verifier_error_raises_authentication_error():
    backend = JWTAuthBackend(_FakeVerifier(error=AuthError("expired", "token expired")))
    with pytest.raises(AuthenticationError) as e:
        _run(backend.authenticate(_conn({"Authorization": "Bearer t"})))
    assert str(e.value) == "expired"


# ---------------------------------------------------------------------------
# auth_on_error → RFC 6750
# ---------------------------------------------------------------------------


def test_on_error_returns_401_with_www_authenticate():
    resp = auth_on_error(MagicMock(), AuthenticationError("expired"))
    assert resp.status_code == 401
    www = resp.headers["WWW-Authenticate"]
    assert www.startswith("Bearer ")
    assert 'error="invalid_token"' in www
    assert "token expired" in www


def test_on_error_unknown_code_raises():
    """Unknown codes must raise — no silent fallback to a generic message."""
    with pytest.raises(KeyError):
        auth_on_error(MagicMock(), AuthenticationError("not_a_real_code"))


# ---------------------------------------------------------------------------
# build_auth_backend factory
# ---------------------------------------------------------------------------


def test_build_disabled_yields_no_verifier():
    backend, owned_http = build_auth_backend(JWTConfig(enabled=False))
    assert isinstance(backend, JWTAuthBackend)
    assert backend._verifier is None
    assert owned_http is None


def test_build_env_source(monkeypatch):
    monkeypatch.setenv("X_SECRET", "x" * 32)
    backend, owned = build_auth_backend(
        JWTConfig(enabled=True, algorithms=["HS256"], key_env="X_SECRET")
    )
    assert backend._verifier is not None
    assert owned is None  # no HTTP client needed


# ---------------------------------------------------------------------------
# JWTPayload / JWTUser sanity (re-tested here so the auth/ package is
# self-coverable without the security.py shim).
# ---------------------------------------------------------------------------


def test_payload_dot_access():
    p = JWTPayload({"sub": "alice", "claims": {"org": 42}})
    assert p.sub == "alice"
    assert p.claims.org == 42


def test_user_authenticated_when_sub_present():
    assert JWTUser(JWTPayload({"sub": "alice"})).is_authenticated is True
    assert JWTUser(JWTPayload({})).is_authenticated is False
