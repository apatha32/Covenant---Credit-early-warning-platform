"""Supabase token verification: signature, issuer, audience, expiry, anonymous flag."""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from tripwire_api.auth import AuthError, TokenVerifier

URL = "https://example.supabase.co"
ISSUER = f"{URL}/auth/v1"
SECRET = "local-test-secret-at-least-32-bytes-long"  # noqa: S105


class _Jwks:
    def __init__(self, key: Any) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return self


def _claims(**over: Any) -> dict[str, Any]:
    return {"sub": str(uuid.uuid4()), "aud": "authenticated", "iss": ISSUER,
            "exp": int(time.time()) + 600, "email": "a@bank.test", **over}  # fmt: skip


@pytest.fixture
def es256() -> tuple[TokenVerifier, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    verifier = TokenVerifier(URL)
    verifier.jwks = _Jwks(key.public_key())  # type: ignore[assignment]
    return verifier, key


def test_es256_token(es256: tuple[TokenVerifier, ec.EllipticCurvePrivateKey]) -> None:
    verifier, key = es256
    claims = _claims()
    p = verifier.verify(jwt.encode(claims, key, algorithm="ES256"))
    assert (str(p.user_id), p.email, p.is_anonymous) == (claims["sub"], "a@bank.test", False)
    anon = verifier.verify(jwt.encode(_claims(is_anonymous=True, email=""), key, "ES256"))
    assert anon.is_anonymous and anon.email is None


@pytest.mark.parametrize("bad", [
    {"aud": "anon"}, {"iss": "https://other.supabase.co/auth/v1"},
    {"exp": int(time.time()) - 10}, {"sub": "service_role"},
])  # fmt: skip
def test_rejects_wrong_claims(
    es256: tuple[TokenVerifier, ec.EllipticCurvePrivateKey], bad: dict[str, Any]
) -> None:
    verifier, key = es256
    with pytest.raises(AuthError):
        verifier.verify(jwt.encode(_claims(**bad), key, algorithm="ES256"))


def test_rejects_other_signing_key(es256: tuple[TokenVerifier, ec.EllipticCurvePrivateKey]) -> None:
    verifier, _ = es256
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(AuthError):
        verifier.verify(jwt.encode(_claims(), other, algorithm="ES256"))


def test_hs256_only_with_configured_secret() -> None:
    token = jwt.encode(_claims(), SECRET, algorithm="HS256")
    assert TokenVerifier(URL, SECRET).verify(token).email == "a@bank.test"
    with pytest.raises(AuthError):
        TokenVerifier(URL).verify(token)  # no secret configured: never trust HS256
    with pytest.raises(AuthError):
        TokenVerifier(URL, "another-secret-that-is-also-32-bytes!").verify(token)
