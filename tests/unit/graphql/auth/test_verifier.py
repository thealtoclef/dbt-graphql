"""Verifier-level unit tests: signature, claims, alg-confusion, leeway, scope."""

from __future__ import annotations

import asyncio
import time

import pytest
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import KeySet, OctKey, RSAKey

from dbt_graphql.graphql.auth.verifier import AuthError, Verifier, extract_scopes


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def _hmac_keyset(secret: str = "x" * 32) -> KeySet:
    return KeySet([OctKey.import_key(secret)])


def _rsa_keyset() -> KeySet:
    return KeySet([RSAKey.generate_key(2048)])


class _StaticResolver:
    def __init__(self, ks: KeySet) -> None:
        self._ks = ks

    async def get(self) -> KeySet:
        return self._ks


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _verifier(
    keyset: KeySet,
    *,
    algorithms=("HS256",),
    audience=None,
    issuer=None,
    leeway=30,
    required_claims=("exp",),
    roles_claim="scope",
) -> Verifier:
    return Verifier(
        key_resolver=_StaticResolver(keyset),
        algorithms=list(algorithms),
        audience=audience,
        issuer=issuer,
        leeway=leeway,
        required_claims=list(required_claims),
        roles_claim=roles_claim,
    )


def _sign(claims: dict, keyset: KeySet, alg: str = "HS256") -> str:
    key = list(keyset)[0]
    return joserfc_jwt.encode({"alg": alg}, claims, key)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_hs256_happy_path():
    ks = _hmac_keyset()
    v = _verifier(ks)
    token = _sign({"sub": "u", "exp": int(time.time()) + 60}, ks)
    claims = _run(v.verify(token))
    assert claims["sub"] == "u"


def test_rs256_happy_path():
    ks = _rsa_keyset()
    v = _verifier(ks, algorithms=("RS256",))
    token = _sign({"sub": "u", "exp": int(time.time()) + 60}, ks, alg="RS256")
    claims = _run(v.verify(token))
    assert claims["sub"] == "u"


# ---------------------------------------------------------------------------
# Algorithm-confusion regression
# ---------------------------------------------------------------------------


def test_alg_confusion_rsa_token_against_hmac_verifier_rejected():
    """RS256 token must not validate against an HS256-pinned verifier."""
    rsa = _rsa_keyset()
    rsa_token = _sign({"sub": "u", "exp": int(time.time()) + 60}, rsa, alg="RS256")
    hmac_verifier = _verifier(_hmac_keyset(), algorithms=("HS256",))
    with pytest.raises(AuthError) as excinfo:
        _run(hmac_verifier.verify(rsa_token))
    assert excinfo.value.code == "invalid_signature"


def test_unlisted_algorithm_rejected():
    ks = _hmac_keyset()
    v = _verifier(ks, algorithms=("HS512",))
    token = _sign({"sub": "u", "exp": int(time.time()) + 60}, ks, alg="HS256")
    with pytest.raises(AuthError) as excinfo:
        _run(v.verify(token))
    assert excinfo.value.code == "invalid_signature"


# ---------------------------------------------------------------------------
# Claims validation
# ---------------------------------------------------------------------------


def test_expired_token_rejected():
    ks = _hmac_keyset()
    v = _verifier(ks, leeway=5)
    token = _sign({"sub": "u", "exp": int(time.time()) - 60}, ks)
    with pytest.raises(AuthError) as e:
        _run(v.verify(token))
    assert e.value.code == "expired"


def test_exp_within_leeway_accepted():
    """Token expired 10s ago, leeway=30 → still valid."""
    ks = _hmac_keyset()
    v = _verifier(ks, leeway=30)
    token = _sign({"sub": "u", "exp": int(time.time()) - 10}, ks)
    claims = _run(v.verify(token))
    assert claims["sub"] == "u"


def test_exp_just_past_leeway_rejected():
    ks = _hmac_keyset()
    v = _verifier(ks, leeway=5)
    token = _sign({"sub": "u", "exp": int(time.time()) - 10}, ks)
    with pytest.raises(AuthError):
        _run(v.verify(token))


def test_audience_mismatch_rejected():
    ks = _hmac_keyset()
    v = _verifier(ks, audience="dbt-graphql")
    token = _sign({"sub": "u", "exp": int(time.time()) + 60, "aud": "other"}, ks)
    with pytest.raises(AuthError) as e:
        _run(v.verify(token))
    assert e.value.code in {"wrong_aud", "invalid_signature"}


def test_audience_match_accepted():
    ks = _hmac_keyset()
    v = _verifier(ks, audience="dbt-graphql")
    token = _sign({"sub": "u", "exp": int(time.time()) + 60, "aud": "dbt-graphql"}, ks)
    assert _run(v.verify(token))["sub"] == "u"


def test_issuer_mismatch_rejected():
    ks = _hmac_keyset()
    v = _verifier(ks, issuer="https://issuer/")
    token = _sign(
        {"sub": "u", "exp": int(time.time()) + 60, "iss": "https://other/"}, ks
    )
    with pytest.raises(AuthError) as e:
        _run(v.verify(token))
    assert e.value.code in {"wrong_iss", "invalid_signature"}


def test_required_claim_missing_rejected():
    ks = _hmac_keyset()
    v = _verifier(ks, required_claims=("sub", "exp"))
    token = _sign({"exp": int(time.time()) + 60}, ks)  # no sub
    with pytest.raises(AuthError):
        _run(v.verify(token))


def test_malformed_token_rejected():
    v = _verifier(_hmac_keyset())
    with pytest.raises(AuthError) as e:
        _run(v.verify("not-even-three-segments"))
    assert e.value.code == "invalid_signature"


# ---------------------------------------------------------------------------
# Scope extraction
# ---------------------------------------------------------------------------


def test_extract_scope_string():
    assert extract_scopes({"scope": "read write"}, "scope") == ["read", "write"]


def test_extract_scope_list():
    assert extract_scopes({"roles": ["admin", "editor"]}, "roles") == [
        "admin",
        "editor",
    ]


def test_extract_scope_custom_claim_namespaced():
    """Auth0-style namespaced claim: literal key lookup, not a path."""
    claims = {"https://acme.com/roles": ["a", "b"]}
    assert extract_scopes(claims, "https://acme.com/roles") == ["a", "b"]


def test_extract_scope_missing_returns_empty():
    assert extract_scopes({}, "scope") == []


def test_extract_scope_unconfigured_claim_does_not_fall_through():
    """If config says `roles`, an `scp` claim is ignored — no chain."""
    assert extract_scopes({"scp": "read"}, "roles") == []
