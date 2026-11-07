"""Shared security test vectors: the JWT (ID token) cases against Nodestro's OIDC client (MUP-ONA MP-A8, VB-7).

``tests/vectors/security-vectors.json`` is a pinned copy of nodestro-runtime's canonical corpus;
``tests/vectors/SOURCE.md`` names its origin, version and SHA-256, and the first test here checks that the copy is
exactly the recorded one (no dependency on the runtime's test package). A vector Nodestro judges differently from the
corpus is listed in ``known_differences`` (platform ``nodestro``) with its reason; the test then asserts the listed
verdict, so a difference that disappears fails until its entry is removed.

Each vector is one ID token, built and signed here with keys generated for the test run (no real keys), and checked by
:meth:`~nodestro_api.identity.OidcClient.verify` against a provider served by ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
from collections import Counter
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
import httpx
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm, get_default_algorithms
import pytest

from nodestro_api.identity import LoginError, OidcClient, OidcSettings

PLATFORM = "nodestro"
VECTORS = Path(__file__).parent / "vectors"
CORPUS_PATH = VECTORS / "security-vectors.json"
CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
CONTEXT: dict[str, Any] = CORPUS["jwt_context"]
DIFFERENCES = {
    item["id"]: item
    for item in CORPUS["known_differences"]
    if item["platform"] == PLATFORM and item["category"] == "jwt"
}


# MUP-ONA T-VEC-3
def test_the_copy_is_the_recorded_version() -> None:
    source = (VECTORS / "SOURCE.md").read_text(encoding="utf-8")
    recorded = re.search(r"^SHA-256: `([0-9a-f]{64})`$", source, re.MULTILINE)
    version = re.search(r"^Version: `(\d+)`$", source, re.MULTILINE)
    assert recorded and version, "tests/vectors/SOURCE.md must name the Version and the SHA-256 of the copy"
    actual = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
    assert actual == recorded.group(1), (
        "tests/vectors/security-vectors.json differs from the copy recorded in tests/vectors/SOURCE.md: replace it "
        "with nodestro-runtime's canonical file and record its version and SHA-256; never edit the copy by hand"
    )
    assert int(version.group(1)) == CORPUS["version"]


# MUP-ONA T-VEC-3
def test_the_jwt_vectors_are_well_formed() -> None:
    ids = Counter(vector["id"] for vector in CORPUS["jwt"])
    assert [name for name, count in ids.items() if count > 1] == []
    assert {vector["expect"] for vector in CORPUS["jwt"]} == {"accept", "reject"}
    for vector in CORPUS["jwt"]:
        assert vector["key_ref"] in CONTEXT["key_refs"], vector["id"]
    for item in CORPUS["known_differences"]:
        assert len(item["reason"]) > 30, item
        if item["category"] == "jwt":
            assert item["actual"] in ("accept", "reject") and item["id"] in ids, item


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Keys:
    """The keys the vectors' ``key_ref`` names, generated for this test run (no real keys)."""

    def __init__(self) -> None:
        self.private: dict[str, Any] = {
            "rsa-a": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "ec-p256": ec.generate_private_key(ec.SECP256R1()),
            "ed25519": ed25519.Ed25519PrivateKey.generate(),
            "rsa-unpublished": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        }
        self.shared = secrets.token_bytes(32)
        self.rsa_a_public_pem = (
            self.private["rsa-a"]
            .public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )

    def jwks(self) -> dict[str, Any]:
        """The provider's published key set: rsa-a, ec-p256 and ed25519 (with their names as kid)."""
        published = (("rsa-a", RSAAlgorithm), ("ec-p256", ECAlgorithm), ("ed25519", OKPAlgorithm))
        keys = []
        for name, algorithm in published:
            jwk = json.loads(algorithm.to_jwk(self.private[name].public_key()))
            keys.append({**jwk, "kid": name, "use": "sig"})
        return {"keys": keys}

    def sign(self, signing_input: bytes, alg: str, key_ref: str) -> bytes:
        if key_ref == "none":
            return b""
        if key_ref in ("hmac-shared", "hmac-rsa-a-public"):
            secret = self.shared if key_ref == "hmac-shared" else self.rsa_a_public_pem
            digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[alg]
            return hmac.new(secret, signing_input, digest).digest()
        algorithm = get_default_algorithms()[alg]
        signature: bytes = algorithm.sign(signing_input, self.private[key_ref])
        return signature


def resolve_claims(value: Any, now: int) -> Any:
    """The corpus placeholders: ``@issuer``, ``@client_id``, ``@nonce``, ``@now`` and ``@now+N`` / ``@now-N``."""
    if isinstance(value, list):
        return [resolve_claims(item, now) for item in value]
    if isinstance(value, dict):
        return {name: resolve_claims(item, now) for name, item in value.items()}
    if isinstance(value, str) and value.startswith("@"):
        moment = re.fullmatch(r"@now(?:([+-])(\d+))?", value)
        if moment:
            offset = int(moment.group(2) or 0)
            return now - offset if moment.group(1) == "-" else now + offset
        return CONTEXT[value[1:]]
    return value


def build_token(vector: dict[str, Any], keys: Keys) -> str:
    header = dict(vector["header"])
    claims = resolve_claims(vector["claims"], int(time.time()))
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}".encode()
    signature = keys.sign(signing_input, header["alg"], vector["key_ref"])
    if vector.get("tamper") == "signature":
        signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    return f"{signing_input.decode()}.{_b64(signature)}"


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


def nodestro_jwt(vector: dict[str, Any], keys: Keys) -> str:
    """``accept`` or ``reject``: Nodestro's verdict on a jwt vector."""
    issuer = CONTEXT["issuer"]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=keys.jwks())
        return httpx.Response(404)

    settings = OidcSettings(
        issuer=issuer,
        client_id=CONTEXT["client_id"],
        client_secret="vector-client-secret",
        redirect_uri="https://studio.vectors.test/api/auth/callback",
        leeway=CONTEXT["leeway_seconds"],
        id_token_max_age=CONTEXT["max_age_seconds"],
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        try:
            OidcClient(settings, http=http).verify(build_token(vector, keys), nonce=CONTEXT["nonce"])
        except LoginError:
            return "reject"
    return "accept"


# MUP-ONA T-VEC-3
def test_the_jwt_context_matches_the_defaults() -> None:
    defaults = OidcSettings(issuer=CONTEXT["issuer"], client_id="c", client_secret="s", redirect_uri="https://s/cb")
    assert (defaults.leeway, defaults.id_token_max_age) == (CONTEXT["leeway_seconds"], CONTEXT["max_age_seconds"])


# MUP-ONA T-VEC-3
@pytest.mark.parametrize("vector", [pytest.param(vector, id=vector["id"]) for vector in CORPUS["jwt"]])
def test_jwt_vector(vector: dict[str, Any], keys: Keys) -> None:
    difference = DIFFERENCES.get(vector["id"])
    expected = difference["actual"] if difference else vector["expect"]
    assert nodestro_jwt(vector, keys) == expected
