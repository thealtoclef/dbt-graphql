"""Starlette authentication backend wrapping the JWT verifier."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import httpx
from joserfc.jwk import KeySet
from loguru import logger
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
)
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from ...config import JWTConfig
from .keys import JWKSResolver, KeyResolver, StaticKeyResolver
from .verifier import AuthError, Verifier, extract_scopes


class VerifierLike(Protocol):
    roles_claim: str

    async def verify(self, token: str) -> dict[str, Any]: ...


class JWTPayload:
    """Dot-access wrapper for a JWT payload dict; missing keys return None."""

    def __init__(self, data: dict) -> None:
        for k, v in data.items():
            object.__setattr__(self, k, JWTPayload(v) if isinstance(v, dict) else v)

    def __getattr__(self, _key: str) -> object:
        return None


class JWTUser(BaseUser):
    def __init__(self, payload: JWTPayload) -> None:
        self.payload = payload

    @property
    def is_authenticated(self) -> bool:
        return self.payload.sub is not None

    @property
    def display_name(self) -> str:
        return str(self.payload.sub or "anon")


_ANON: tuple[AuthCredentials, JWTUser] = (
    AuthCredentials([]),
    JWTUser(JWTPayload({})),
)


class JWTAuthBackend(AuthenticationBackend):
    """Starlette backend; ``verifier=None`` ⇒ verification disabled (anonymous)."""

    def __init__(self, verifier: VerifierLike | None) -> None:
        self._verifier = verifier

    async def authenticate(
        self, conn: HTTPConnection
    ) -> tuple[AuthCredentials, JWTUser]:
        if self._verifier is None:
            return _ANON

        auth = conn.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _ANON

        try:
            claims = await self._verifier.verify(token)
        except AuthError as exc:
            raise AuthenticationError(exc.code) from exc

        user = JWTUser(JWTPayload(claims))
        scopes = ["authenticated", *extract_scopes(claims, self._verifier.roles_claim)]
        return AuthCredentials(scopes), user


_DESCRIPTION_BY_CODE: dict[str, str] = {
    "expired": "token expired",
    "wrong_aud": "audience mismatch",
    "wrong_iss": "issuer mismatch",
    "invalid_signature": "invalid token",
    "invalid_claims": "invalid claims",
    "jwks_fetch_failure": "key set unavailable",
}


def auth_on_error(_conn: HTTPConnection, exc: AuthenticationError) -> Response:
    """RFC 6750 401 with WWW-Authenticate. Wires onto AuthenticationMiddleware."""
    code = str(exc)
    description = _DESCRIPTION_BY_CODE[code]
    return JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer error="invalid_token", error_description="{description}"'
            )
        },
    )


def _build_resolver(cfg: JWTConfig, http: httpx.AsyncClient | None) -> KeyResolver:
    if cfg.jwks_url is not None:
        assert http is not None
        return JWKSResolver(str(cfg.jwks_url), cfg.jwks_cache_ttl, http)
    if cfg.key_url is not None:
        assert http is not None
        return _LazyURLResolver(str(cfg.key_url), http)
    if cfg.key_env is not None:
        return StaticKeyResolver.from_env(cfg.key_env)
    assert cfg.key_file is not None
    return StaticKeyResolver.from_file(cfg.key_file)


class _LazyURLResolver:
    """Fetches a single static key on first call, caches it for the process.

    ``_build_resolver`` is synchronous but ``StaticKeyResolver.from_url`` is
    async, so the fetch is deferred to the first ``get()``. Concurrent first
    calls coalesce on a lock so we make at most one HTTP fetch.

    JWKS-shaped URLs are rejected by ``JWTConfig`` validation, so this path
    is guaranteed to be a static PEM/JWK — no rotation, no refresh.
    """

    def __init__(self, url: str, http: httpx.AsyncClient) -> None:
        self._url = url
        self._http = http
        self._inner: StaticKeyResolver | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> KeySet:
        if self._inner is None:
            async with self._lock:
                if self._inner is None:
                    self._inner = await StaticKeyResolver.from_url(
                        self._url, self._http
                    )
        return await self._inner.get()


def build_auth_backend(
    cfg: JWTConfig, *, http_client: httpx.AsyncClient | None = None
) -> tuple[JWTAuthBackend, httpx.AsyncClient | None]:
    """Construct the backend (and the http client it owns, for lifespan close).

    Returns the backend plus the http client created for it (or None if the
    caller passed one in or no HTTP transport is needed).
    """
    if not cfg.enabled:
        logger.warning(
            "JWT verification disabled (security.jwt.enabled=false); every "
            "request will be treated as anonymous"
        )
        return JWTAuthBackend(None), None

    owned: httpx.AsyncClient | None = None
    needs_http = cfg.jwks_url is not None or cfg.key_url is not None
    if needs_http and http_client is None:
        http_client = owned = httpx.AsyncClient()
    resolver = _build_resolver(cfg, http_client if needs_http else None)
    verifier = Verifier(
        key_resolver=resolver,
        algorithms=cfg.algorithms,
        audience=cfg.audience,
        issuer=cfg.issuer,
        leeway=cfg.leeway,
        required_claims=cfg.required_claims,
        roles_claim=cfg.roles_claim,
    )
    return JWTAuthBackend(verifier), owned


__all__ = [
    "JWTAuthBackend",
    "JWTPayload",
    "JWTUser",
    "auth_on_error",
    "build_auth_backend",
]
