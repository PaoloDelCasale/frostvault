"""A self-contained fake OIDC provider for tests.

Provides an OpenID discovery document, a JWKS, and RS256-signed id_tokens,
plus an ``httpx.MockTransport`` that routes discovery/jwks/token requests so
the OIDC module can be driven end-to-end without a real network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import warnings
from typing import Any

import httpx

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, jwt

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def at_hash_for(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return _b64url(digest[: len(digest) // 2])


class FakeOidcProvider:
    def __init__(
        self,
        *,
        issuer: str = "https://fake-oidc.example",
        client_id: str = "test-client",
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.access_token = "fake-access-token"
        self.kid = "test-key"
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key = JsonWebKey.import_key(self._pem, {"kty": "RSA", "kid": self.kid})
        self._public_jwk = key.as_dict(is_private=False)
        self.token_requests: list[dict[str, str]] = []

    @property
    def discovery_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"

    def discovery(self) -> dict[str, Any]:
        base = self.issuer.rstrip("/")
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "jwks_uri": f"{base}/jwks",
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self._public_jwk]}

    def make_id_token(
        self,
        *,
        nonce: str,
        subject: str = "subject-123",
        issuer: str | None = None,
        audience: str | None = None,
        expires_in: int = 300,
        access_token: str | None = None,
        at_hash: str | None = "auto",
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.issuer if issuer is None else issuer,
            "sub": subject,
            "aud": self.client_id if audience is None else audience,
            "iat": now,
            "exp": now + expires_in,
            "nonce": nonce,
        }
        if at_hash == "auto":
            payload["at_hash"] = at_hash_for(access_token or self.access_token)
        elif at_hash is not None:
            payload["at_hash"] = at_hash
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            token = jwt.encode({"alg": "RS256", "kid": self.kid}, payload, self._pem)
        return token.decode("ascii")

    def transport(self, *, id_token: str | None = None) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == self.discovery_url:
                return httpx.Response(200, json=self.discovery())
            if url == self.discovery()["jwks_uri"]:
                return httpx.Response(200, json=self.jwks())
            if url == self.discovery()["token_endpoint"]:
                form = dict(_parse_form(request.content))
                self.token_requests.append(form)
                return httpx.Response(
                    200,
                    json={
                        "access_token": self.access_token,
                        "token_type": "Bearer",
                        "id_token": id_token or "",
                    },
                )
            return httpx.Response(404, json={"error": "not_found", "url": url})

        return httpx.MockTransport(handler)

    def client(self, *, id_token: str | None = None) -> httpx.Client:
        return httpx.Client(transport=self.transport(id_token=id_token))


def _parse_form(content: bytes) -> list[tuple[str, str]]:
    from urllib.parse import parse_qsl

    return parse_qsl(content.decode("ascii"))
