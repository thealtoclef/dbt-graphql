"""JWT verifier — joserfc-backed signature + claims validation."""

from __future__ import annotations

import time
from typing import Any

from joserfc import jwt
from joserfc.errors import (
    ExpiredTokenError,
    InvalidClaimError,
    JoseError,
    MissingClaimError,
)
from joserfc.jwt import JWTClaimsRegistry
from opentelemetry import metrics, trace

from .keys import KeyResolver

_meter = metrics.get_meter(__name__)
_jwt_outcomes = _meter.create_counter(
    "auth.jwt", description="JWT verification outcomes by attribute"
)
_tracer = trace.get_tracer(__name__)


class AuthError(Exception):
    """Verification failed; backend converts this to a 401."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # outcome label: invalid_signature, expired, wrong_aud, etc.


def _build_claims_registry(
    *,
    audience: str | list[str] | None,
    issuer: str | None,
    leeway: int,
    required_claims: list[str],
) -> JWTClaimsRegistry:
    options: dict[str, Any] = {}
    for claim in required_claims:
        options.setdefault(claim, {})["essential"] = True
    if audience is not None:
        options.setdefault("aud", {})["essential"] = True
        if isinstance(audience, list):
            options["aud"]["values"] = audience
        else:
            options["aud"]["value"] = audience
    if issuer is not None:
        options.setdefault("iss", {})["essential"] = True
        options["iss"]["value"] = issuer
    return JWTClaimsRegistry(now=lambda: int(time.time()), leeway=leeway, **options)


class Verifier:
    """One verifier per app: fixed key resolver, algorithms, claim policy."""

    def __init__(
        self,
        *,
        key_resolver: KeyResolver,
        algorithms: list[str],
        audience: str | list[str] | None,
        issuer: str | None,
        leeway: int,
        required_claims: list[str],
        roles_claim: str,
    ) -> None:
        self._key_resolver = key_resolver
        self._algorithms = algorithms
        self._claims_registry = _build_claims_registry(
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            required_claims=required_claims,
        )
        self.roles_claim = roles_claim

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            keyset = await self._key_resolver.get()
        except Exception as exc:
            self._record("jwks_fetch_failure")
            raise AuthError("jwks_fetch_failure", "key fetch failed") from exc

        try:
            decoded = jwt.decode(token, keyset, algorithms=self._algorithms)
        except JoseError as exc:
            self._record("invalid_signature")
            raise AuthError("invalid_signature", "invalid token") from exc

        try:
            self._claims_registry.validate(decoded.claims)
        except ExpiredTokenError as exc:
            self._record("expired")
            raise AuthError("expired", "token expired") from exc
        except (InvalidClaimError, MissingClaimError) as exc:
            outcome = _classify_claim_error(exc)
            self._record(outcome)
            raise AuthError(outcome, str(exc)) from exc

        self._record("success", decoded.claims)
        return decoded.claims

    def _record(self, outcome: str, claims: dict[str, Any] | None = None) -> None:
        _jwt_outcomes.add(1, {"outcome": outcome})
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("auth.jwt.outcome", outcome)
            if claims:
                for k in ("kid", "iss", "sub"):
                    if (v := claims.get(k)) is not None:
                        span.set_attribute(f"auth.jwt.{k}", str(v))


def _classify_claim_error(exc: InvalidClaimError | MissingClaimError) -> str:
    claim = getattr(exc, "claim", None)
    if claim == "aud":
        return "wrong_aud"
    if claim == "iss":
        return "wrong_iss"
    return "invalid_claims"


def extract_scopes(claims: dict[str, Any], roles_claim: str) -> list[str]:
    """Read scopes from ``claims[roles_claim]``: space-delimited string or list."""
    raw = claims.get(roles_claim)
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []
