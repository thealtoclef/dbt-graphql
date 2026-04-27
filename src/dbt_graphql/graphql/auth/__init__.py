"""JWT authentication: backend, verifier, key resolvers."""

from .backend import (
    JWTAuthBackend,
    JWTPayload,
    JWTUser,
    auth_on_error,
    build_auth_backend,
)
from .verifier import AuthError, Verifier

__all__ = [
    "AuthError",
    "JWTAuthBackend",
    "JWTPayload",
    "JWTUser",
    "Verifier",
    "auth_on_error",
    "build_auth_backend",
]
