"""Pairing a browser with a portable package by a one-time code, and the device rights it creates (NAL-C4).

The protocol and its threat model are in ``docs/pairing.md``. In short: the operator asks the running application for
a code (8 characters of Crockford Base32, 10 minutes, one use, 5 attempts); a browser that sends it over TLS (or from
loopback) gets a device right with fixed app-user scopes, a session and a rotating device secret that renews the
session. Pairing is off unless ``NODESTRO_PAIRING=on`` (decision E-7, until the independent review D1).

Codes live only in the memory of the running process and only as hashes; devices are kept in ``devices.json`` in the
package's data folder (hashes of their secrets, never the secrets).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

#: Crockford's Base32 alphabet (no I, L, O, U).
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
CODE_MINUTES = 10
CODE_ATTEMPTS = 5
#: Failed pairing attempts per client address and hour.
ADDRESS_FAILURES = 20
MAX_OPEN_CODES = 5
DEVICE_DAYS = 90
NAME_LENGTH = 60
SCOPES = ("run", "read", "approve")
DEFAULT_SCOPES = ("run", "read")
DEVICES_FILE = "devices.json"
DEVICE_COOKIE = "nodestro_device"
DEVICE_PATH = "/ui/api/device"


def enabled(environ: dict[str, str] | None = None) -> bool:
    """Pairing is off unless ``NODESTRO_PAIRING`` is ``on`` (or ``1``, ``true``)."""
    value = (environ if environ is not None else os.environ).get("NODESTRO_PAIRING", "")
    return value.strip().lower() in ("1", "on", "true", "yes")


class PairingError(Exception):
    """Why a pairing or a device renewal was refused: ``key`` is the API code, ``status`` the HTTP status."""

    def __init__(self, status: int, key: str, message: str) -> None:
        super().__init__(message)
        self.status, self.key = status, key


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize(code: str) -> str:
    """A typed code in canonical form: upper case, without separators, I/L as 1 and O as 0 (Crockford)."""
    text = "".join(code.split()).replace("-", "").upper()
    return text.translate(str.maketrans("ILO", "110"))


def display(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short(device_id: str) -> str:
    """A device id as logs show it."""
    return f"{device_id[:8]}…"


@dataclass
class _Code:
    digest: str
    name: str
    scopes: list[str]
    expires: float
    attempts: int = 0


class OpenCode(BaseModel):
    """A new code as the launcher shows it (once; only its hash is kept)."""

    code: str
    expires_at: datetime
    scopes: list[str]
    name: str


class Device(BaseModel):
    """A paired browser: an app user with fixed scopes for the package's app (never the operator)."""

    id: str
    name: str
    scopes: list[str]
    created_at: datetime
    last_seen: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    #: The browser and system as its user agent named them, shortened (for the device list only).
    agent: str = ""
    secret_hash: str = ""
    #: Hashes of secrets that rotation replaced: presenting one again revokes the device (stolen cookie).
    replaced: list[str] = Field(default_factory=list)

    def active(self, moment: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > moment

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"secret_hash", "replaced"})


def _now() -> datetime:
    return datetime.now(UTC)


class Pairing:
    """Open codes (memory) and devices (``devices.json`` in ``data_dir``) of one running application."""

    def __init__(self, data_dir: Path, *, clock: Any = time.monotonic) -> None:
        self.path = data_dir / DEVICES_FILE
        self._clock = clock
        self._codes: list[_Code] = []
        self._failures: dict[str, deque[float]] = {}
        self._cache: tuple[tuple[int, int], list[Device]] | None = None
        self._lock = threading.Lock()

    # ---- codes ----------------------------------------------------------------------------------------------------

    def create_code(self, *, name: str = "", scopes: list[str] | None = None, minutes: int = CODE_MINUTES) -> OpenCode:
        chosen = list(dict.fromkeys(scopes or DEFAULT_SCOPES))
        if not chosen or any(scope not in SCOPES for scope in chosen):
            raise PairingError(400, "pairing.scopes_invalid", f"Scopes are a subset of {', '.join(SCOPES)}")
        minutes = max(1, min(int(minutes), CODE_MINUTES))
        code = new_code()
        with self._lock:
            self._prune()
            if len(self._codes) >= MAX_OPEN_CODES:
                raise PairingError(429, "pairing.too_many_codes", "Too many open codes; wait until one expires")
            self._codes.append(_Code(_hash(code), name.strip()[:NAME_LENGTH], chosen, self._clock() + minutes * 60))
        return OpenCode(
            code=code, expires_at=_now() + timedelta(minutes=minutes), scopes=chosen, name=name.strip()[:NAME_LENGTH]
        )

    def _prune(self) -> None:
        moment = self._clock()
        self._codes = [item for item in self._codes if item.expires > moment and item.attempts < CODE_ATTEMPTS]
        for address in list(self._failures):
            window = self._failures[address]
            while window and window[0] < moment - 3600:
                window.popleft()
            if not window:
                del self._failures[address]

    def _fail(self, address: str) -> None:
        self._failures.setdefault(address, deque()).append(self._clock())

    def redeem(self, code: str, *, address: str, name: str = "", agent: str = "") -> tuple[Device, str]:
        """Consume ``code`` and create a device; returns it and its first secret."""
        typed = normalize(code)
        with self._lock:
            self._prune()
            if len(self._failures.get(address, ())) >= ADDRESS_FAILURES:
                raise PairingError(429, "pairing.too_many_attempts", "Too many wrong codes; try again later")
            digest = _hash(typed)
            found = None
            for item in self._codes:
                if hmac.compare_digest(item.digest, digest):
                    found = item
            if found is None:
                # A wrong code counts against every open code (5 attempts each) and against the address.
                for item in self._codes:
                    item.attempts += 1
                self._fail(address)
                raise PairingError(400, "pairing.code_invalid", "The code is wrong or expired")
            self._codes.remove(found)
        moment = _now()
        secret = secrets.token_urlsafe(32)
        device = Device(
            id=f"dev_{secrets.token_hex(8)}",
            name=(name.strip() or found.name or "Browser")[:NAME_LENGTH],
            scopes=found.scopes,
            created_at=moment,
            last_seen=moment,
            expires_at=moment + timedelta(days=DEVICE_DAYS),
            agent=agent[:120],
            secret_hash=_hash(secret),
        )
        with self._lock:
            devices = self._load()
            devices.append(device)
            self._save(devices)
        return device, secret

    # ---- devices --------------------------------------------------------------------------------------------------

    def _load(self) -> list[Device]:
        """The devices of ``devices.json`` (read again only when the file changed: every device request asks)."""
        try:
            stat = self.path.stat()
        except OSError:
            return []
        mark = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == mark:
            return [item.model_copy(deep=True) for item in self._cache[1]]
        try:
            data = json.loads(self.path.read_text("utf-8"))
            devices = [Device.model_validate(item) for item in data.get("devices", []) if isinstance(item, dict)]
        except (OSError, ValueError, AttributeError):
            # A damaged file pairs nothing: the devices must pair again (never a crash of the application).
            return []
        self._cache = (mark, devices)
        return [item.model_copy(deep=True) for item in devices]

    def _save(self, devices: list[Device]) -> None:
        from .portable_ops import restrict_folder

        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".partial")
        text = json.dumps({"format": 1, "devices": [item.model_dump(mode="json") for item in devices]}, indent=2)
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(partial, self.path)
        if os.name == "nt":
            restrict_folder(self.path.parent)

    def devices(self) -> list[Device]:
        with self._lock:
            return self._load()

    def device(self, device_id: str) -> Device | None:
        """The device if it may still be used (not revoked, not expired)."""
        moment = _now()
        return next((item for item in self.devices() if item.id == device_id and item.active(moment)), None)

    def renew(self, cookie: str) -> tuple[Device, str]:
        """A session from the device cookie ``<id>.<secret>``: the secret rotates; an old secret revokes the device."""
        device_id, _, secret = cookie.partition(".")
        refused = PairingError(401, "pairing.device_revoked", "This browser is no longer paired; pair it again")
        if not device_id or not secret:
            raise refused
        digest = _hash(secret)
        with self._lock:
            devices = self._load()
            device = next((item for item in devices if item.id == device_id), None)
            moment = _now()
            if device is None or not device.active(moment):
                raise refused
            if any(hmac.compare_digest(old, digest) for old in device.replaced):
                device.revoked_at = moment
                self._save(devices)
                raise refused
            if not hmac.compare_digest(device.secret_hash, digest):
                raise refused
            fresh = secrets.token_urlsafe(32)
            device.replaced = [*device.replaced, device.secret_hash][-20:]
            device.secret_hash = _hash(fresh)
            device.last_seen = moment
            device.expires_at = moment + timedelta(days=DEVICE_DAYS)
            self._save(devices)
        return device, fresh

    def revoke(self, device_id: str | None = None) -> int:
        """Revoke one device (``device_id``) or every device (``None``); returns how many were revoked."""
        with self._lock:
            devices = self._load()
            moment = _now()
            count = 0
            for item in devices:
                if item.revoked_at is None and (device_id is None or item.id == device_id):
                    item.revoked_at = moment
                    count += 1
            if count:
                self._save(devices)
        return count


def revoke_offline(data_dir: Path, device_id: str | None) -> int:
    """``devices revoke`` while the application is stopped: change ``devices.json`` directly."""
    return Pairing(data_dir).revoke(device_id)
