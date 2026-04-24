from __future__ import annotations

import jwt
from starlette.authentication import AuthCredentials, AuthenticationBackend, BaseUser
from starlette.requests import HTTPConnection


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


class JWTAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection):
        auth = conn.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return AuthCredentials([]), JWTUser(JWTPayload({}))
        try:
            payload = jwt.decode(
                auth[len("Bearer ") :], options={"verify_signature": False}
            )
        except jwt.exceptions.DecodeError:
            payload = {}
        user = JWTUser(JWTPayload(payload))
        scopes = ["authenticated"] if user.is_authenticated else []
        return AuthCredentials(scopes), user
