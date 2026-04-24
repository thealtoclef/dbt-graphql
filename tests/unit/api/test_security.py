from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import jwt as pyjwt

from dbt_graphql.api.security import JWTAuthBackend, JWTPayload, JWTUser


def _jwt(payload: dict, secret: str = "secret") -> str:
    return pyjwt.encode(payload, secret, algorithm="HS256")


def _conn(headers: dict | None = None):
    m = MagicMock()
    m.headers = headers or {}
    return m


# ---------------------------------------------------------------------------
# JWTPayload
# ---------------------------------------------------------------------------


def test_payload_dot_access():
    p = JWTPayload({"sub": "alice", "claims": {"org": 42}})
    assert p.sub == "alice"
    assert p.claims.org == 42


def test_payload_missing_key_returns_none():
    assert JWTPayload({}).sub is None


def test_payload_nested_missing_returns_none():
    assert JWTPayload({"claims": {}}).claims.region is None


# ---------------------------------------------------------------------------
# JWTUser
# ---------------------------------------------------------------------------


def test_user_authenticated_when_sub_present():
    assert JWTUser(JWTPayload({"sub": "alice"})).is_authenticated is True


def test_user_not_authenticated_without_sub():
    assert JWTUser(JWTPayload({})).is_authenticated is False


def test_user_display_name_is_sub():
    assert JWTUser(JWTPayload({"sub": "alice"})).display_name == "alice"


def test_user_display_name_falls_back_to_anon():
    assert JWTUser(JWTPayload({})).display_name == "anon"


# ---------------------------------------------------------------------------
# JWTAuthBackend
# ---------------------------------------------------------------------------


def test_backend_no_header_returns_unauthenticated():
    creds, user = asyncio.run(JWTAuthBackend().authenticate(_conn()))
    assert not user.is_authenticated
    assert "authenticated" not in creds.scopes


def test_backend_valid_bearer_decodes_claims():
    token = _jwt({"sub": "u1", "groups": ["analysts"], "claims": {"org": 42}})
    creds, user = asyncio.run(
        JWTAuthBackend().authenticate(_conn({"Authorization": f"Bearer {token}"}))
    )
    assert user.is_authenticated
    assert user.payload.sub == "u1"
    assert user.payload.groups == ["analysts"]
    assert user.payload.claims.org == 42
    assert "authenticated" in creds.scopes


def test_backend_non_bearer_scheme_ignored():
    _, user = asyncio.run(
        JWTAuthBackend().authenticate(_conn({"Authorization": "Basic dXNlcjpwYXNz"}))
    )
    assert not user.is_authenticated


def test_backend_malformed_token_degrades_to_anon():
    creds, user = asyncio.run(
        JWTAuthBackend().authenticate(_conn({"Authorization": "Bearer garbage"}))
    )
    assert not user.is_authenticated
    assert "authenticated" not in creds.scopes
