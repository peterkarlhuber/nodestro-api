"""A stub OpenID Connect provider for tests: discovery, keys and a token endpoint behind ``httpx.MockTransport``.

It checks PKCE like a real provider (the code verifier must match the challenge of the authorization request) and
signs ID tokens with its own RSA key; tests can change the claims or the key to produce invalid tokens.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://login.example"
CLIENT_ID = "nodestro-studio"
CLIENT_SECRET = "test-client-secret"
ORIGIN = "https://studio.test"
REDIRECT_URI = f"{ORIGIN}/api/auth/callback"


@dataclass
class _Grant:
    subject: str
    nonce: str
    challenge: str
    claims: dict[str, Any]


@dataclass
class StubProvider:
    key: Any = field(default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048))
    kid: str = "key-1"
    grants: dict[str, _Grant] = field(default_factory=dict)
    #: Changes applied to the next ID token (e.g. ``{"aud": "other"}``), then cleared.
    tamper: dict[str, Any] = field(default_factory=dict)
    #: Sign the next token with this key instead (a forged token), then cleared.
    forge_with: Any = None
    token_requests: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if url == f"{ISSUER}/jwks":
            jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]})
        if url == f"{ISSUER}/token" and request.method == "POST":
            return self._token(request)
        return httpx.Response(404)

    def authorize(self, location: str, subject: str, **claims: Any) -> str:
        """What the provider does after the person logged in: the callback URL with a fresh code."""
        query = {key: values[0] for key, values in parse_qs(urlparse(location).query).items()}
        assert location.startswith(f"{ISSUER}/authorize?")
        assert query["client_id"] == CLIENT_ID and query["redirect_uri"] == REDIRECT_URI
        assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
        code = secrets.token_urlsafe(16)
        self.grants[code] = _Grant(subject, query["nonce"], query["code_challenge"], claims)
        return f"/api/auth/callback?code={code}&state={query['state']}"

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        grant = self.grants.pop(form.get("code", ""), None)
        if grant is None or form.get("redirect_uri") != REDIRECT_URI:
            return httpx.Response(400, json={"error": "invalid_grant"})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
        if challenge.decode() != grant.challenge:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": grant.subject,
            "nonce": grant.nonce,
            "iat": now,
            "exp": now + 300,
            **grant.claims,
            **self.tamper,
        }
        claims = {key: value for key, value in claims.items() if value is not None}
        key = self.forge_with or self.key
        self.tamper, self.forge_with = {}, None
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})
        return httpx.Response(200, json={"id_token": token, "access_token": "unused", "token_type": "Bearer"})
