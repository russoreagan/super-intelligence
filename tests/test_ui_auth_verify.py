"""
Token-verification tests for the auth gate (brain/ui/auth.py).

The gateway crashed/wedged in prod because it could only verify HS256 while the
Supabase project signs with ES256, so every request fell back to a remote GoTrue
call. These tests pin the local-verification contract:
  • ES256 tokens verify locally against the cached JWKS public key (no network)
  • a token signed by the wrong key, an expired token, and a malformed token are
    all rejected locally WITHOUT a remote round-trip
  • legacy HS256 still works against the shared secret
  • the remote GoTrue fallback is used ONLY when local verification is impossible
"""

from __future__ import annotations

import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from brain.ui import auth

_KID = "test-kid-1"


def _es256_keypair_and_jwk(kid: str = _KID):
    """Return (private_key, PyJWK) for an ES256 (P-256) signing key."""
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk_dict = json.loads(ECAlgorithm.to_jwk(priv.public_key()))
    jwk_dict.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return priv, jwt.PyJWK(jwk_dict)


def _install_jwks(monkeypatch, kid, pyjwk):
    """Seed the in-memory JWKS cache so verification is purely local (no fetch)."""
    monkeypatch.setattr(auth, "_jwks_cache", {kid: pyjwk})
    monkeypatch.setattr(auth, "_jwks_fetched_at", time.monotonic())


def _es256_token(priv, kid=_KID, *, sub="user-123", exp_delta=3600, aud="authenticated"):
    payload = {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta}
    return jwt.encode(payload, priv, algorithm="ES256", headers={"kid": kid})


class TestLocalES256:
    async def test_valid_token_verifies_locally(self, monkeypatch):
        priv, pyjwk = _es256_keypair_and_jwk()
        _install_jwks(monkeypatch, _KID, pyjwk)
        # If anything tries the remote path, fail loudly.
        monkeypatch.setattr(auth, "_verify_remote", _no_remote)

        claims = await auth._verify_access(_es256_token(priv))
        assert claims is not None
        assert claims["sub"] == "user-123"

    async def test_wrong_key_rejected_without_remote(self, monkeypatch):
        # Cache holds the REAL key; token is signed by a DIFFERENT key with the same kid.
        _, real_jwk = _es256_keypair_and_jwk()
        impostor_priv, _ = _es256_keypair_and_jwk()
        _install_jwks(monkeypatch, _KID, real_jwk)
        monkeypatch.setattr(auth, "_verify_remote", _no_remote)

        assert await auth._verify_access(_es256_token(impostor_priv)) is None

    async def test_expired_token_rejected_without_remote(self, monkeypatch):
        priv, pyjwk = _es256_keypair_and_jwk()
        _install_jwks(monkeypatch, _KID, pyjwk)
        monkeypatch.setattr(auth, "_verify_remote", _no_remote)

        assert await auth._verify_access(_es256_token(priv, exp_delta=-60)) is None

    async def test_wrong_audience_rejected(self, monkeypatch):
        priv, pyjwk = _es256_keypair_and_jwk()
        _install_jwks(monkeypatch, _KID, pyjwk)
        monkeypatch.setattr(auth, "_verify_remote", _no_remote)

        assert await auth._verify_access(_es256_token(priv, aud="anon")) is None

    async def test_malformed_token_rejected(self, monkeypatch):
        monkeypatch.setattr(auth, "_verify_remote", _no_remote)
        assert await auth._verify_access("not.a.jwt") is None
        assert await auth._verify_access("garbage") is None


class TestLocalHS256:
    async def test_hs256_verifies_against_secret(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_JWT_SECRET", "shh-secret")
        monkeypatch.setattr(auth, "_verify_remote", _no_remote)
        token = jwt.encode(
            {"sub": "u9", "aud": "authenticated", "exp": int(time.time()) + 3600},
            "shh-secret",
            algorithm="HS256",
        )
        claims = await auth._verify_access(token)
        assert claims is not None and claims["sub"] == "u9"


class TestRemoteFallback:
    async def test_falls_back_when_jwks_unavailable(self, monkeypatch):
        # No key cached and refresh yields nothing → must use the remote check once.
        priv, _ = _es256_keypair_and_jwk()
        monkeypatch.setattr(auth, "_jwks_cache", {})
        monkeypatch.setattr(auth, "_jwks_fetched_at", 0.0)

        async def _fake_refresh():
            return None  # JWKS endpoint unreachable

        called = {}

        async def _fake_remote(token):
            called["yes"] = True
            return {"sub": "remote-user"}

        monkeypatch.setattr(auth, "_refresh_jwks", _fake_refresh)
        monkeypatch.setattr(auth, "_verify_remote", _fake_remote)

        claims = await auth._verify_access(_es256_token(priv))
        assert called.get("yes") is True
        assert claims == {"sub": "remote-user"}


async def _no_remote(token):
    raise AssertionError("remote GoTrue verification should not be called on the local path")
