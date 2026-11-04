"""Supabase access-token verification.

Hosted projects sign tokens with an asymmetric key (ES256 / RS256) published at
{SUPABASE_URL}/auth/v1/.well-known/jwks.json; a local Supabase may still use a shared HS256
secret (SUPABASE_JWT_SECRET). Either way the token must be unexpired, issued by this project's
auth server, and meant for signed-in clients (aud = authenticated).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import jwt


class AuthError(Exception):
    """The request carries no valid Supabase access token."""


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str | None
    is_anonymous: bool


class TokenVerifier:
    def __init__(self, supabase_url: str, jwt_secret: str | None = None) -> None:
        self.issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self.secret = jwt_secret
        self.jwks = jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", lifespan=3600)

    def verify(self, token: str) -> Principal:
        try:
            alg = jwt.get_unverified_header(token).get("alg", "")
            if alg.startswith("HS"):
                if not self.secret:
                    raise AuthError("HS256 tokens need SUPABASE_JWT_SECRET")
                key: Any = self.secret
                algorithms = ["HS256"]
            else:
                key = self.jwks.get_signing_key_from_jwt(token).key
                algorithms = ["ES256", "RS256"]
            claims = jwt.decode(
                token, key, algorithms=algorithms, audience="authenticated", issuer=self.issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )  # fmt: skip
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise AuthError("token subject is not a user id") from exc
        return Principal(user_id, claims.get("email") or None, bool(claims.get("is_anonymous")))
