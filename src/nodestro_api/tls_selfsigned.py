"""A self-issued TLS certificate for a portable package in the network (NAL-C4, decision E-7).

Only for ``server.tls_self_signed`` when the operator configured neither an own certificate nor a TLS proxy. The
browser cannot check such a certificate against an authority: a person compares the SHA-256 fingerprint the launcher
prints with the one the browser shows, and the interface marks the connection as "restricted trust" for as long as it
is used. See ``docs/pairing.md``.

Needs the ``cryptography`` library, which a package contains where a ready-built package for its platform exists
(not on macOS Intel and Windows ARM64): :func:`available` says whether it can be used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl

FOLDER = Path("secrets") / "tls"
CERT = "self-signed.crt"
KEY = "self-signed.key"
INFO = "self-signed.json"
DAYS = 397
RENEW_DAYS = 30


def available() -> bool:
    try:
        import cryptography.x509  # noqa: F401
    except ImportError:
        return False
    return True


def fingerprint(cert_pem: bytes) -> str:
    """The SHA-256 of the certificate's DER encoding as browsers show it: ``AB:CD:…`` (32 pairs)."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem.decode("ascii"))).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def local_names() -> tuple[list[str], list[str]]:
    """This computer's host names and IP addresses (for the certificate's subject alternative names)."""
    names = {"localhost", socket.gethostname().lower()}
    addresses = {"127.0.0.1", "::1"}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            addresses.add(str(item[4][0]).split("%", 1)[0])
    except OSError:
        pass
    # The address of the default route (no packet is sent by a UDP connect).
    for target in ("192.0.2.1", "2001:db8::1"):
        family = socket.AF_INET6 if ":" in target else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 9))
                addresses.add(str(probe.getsockname()[0]).split("%", 1)[0])
        except OSError:
            pass
    return sorted(name for name in names if name), sorted(addresses)


def ensure(data_dir: Path, *, now: datetime | None = None) -> tuple[Path, Path, str]:
    """The certificate and key in ``data_dir/secrets/tls``, created or renewed when missing, expiring within 30 days
    or not covering this computer's current names; returns their paths and the fingerprint."""
    folder = data_dir / FOLDER
    cert, key, info = folder / CERT, folder / KEY, folder / INFO
    moment = now or datetime.now(UTC)
    names, addresses = local_names()
    try:
        recorded = json.loads(info.read_text("utf-8"))
        current = (
            cert.is_file()
            and key.is_file()
            and datetime.fromisoformat(recorded["not_after"]) - moment > timedelta(days=RENEW_DAYS)
            and set(names) <= set(recorded["names"])
            and set(addresses) <= set(recorded["addresses"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        current = False
    if not current:
        _create(folder, names, addresses, moment)
    return cert, key, fingerprint(cert.read_bytes())


def _create(folder: Path, names: list[str], addresses: list[str], moment: datetime) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from .portable_ops import restrict_folder

    folder.mkdir(parents=True, exist_ok=True)
    restrict_folder(folder)
    private = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[-1] if names else "localhost")])
    alternatives: list[x509.GeneralName] = [x509.DNSName(name) for name in names]
    alternatives += [x509.IPAddress(ipaddress.ip_address(address)) for address in addresses]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - timedelta(minutes=5))
        .not_valid_after(moment + timedelta(days=DAYS))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private, hashes.SHA256())
    )
    key_bytes = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    _write(folder / KEY, key_bytes, 0o600)
    _write(folder / CERT, certificate.public_bytes(serialization.Encoding.PEM), 0o644)
    record = {"not_after": (moment + timedelta(days=DAYS)).isoformat(), "names": names, "addresses": addresses}
    _write(folder / INFO, json.dumps(record, indent=2).encode("utf-8"), 0o644)


def _write(path: Path, data: bytes, mode: int) -> None:
    partial = path.with_name(path.name + ".partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(partial, path)
