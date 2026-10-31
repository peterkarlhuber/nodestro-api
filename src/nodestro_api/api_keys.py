"""API keys for published workflows: Nodestro's own keys, independent of the Anthropic key.

A key looks like ``ndk_<id>_<secret>``. It is shown exactly once, when it is created or
replaced; the database keeps only its id, a SHA-256 hash of the secret, its permissions
(``run``, ``read``, ``approve``) and the publications it may use. A key never grants access
to other workflows or to the studio. Revoked keys stay listed (for the record) and are
rejected at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import threading

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from nodestro_runtime.storage import DocumentStore
from pydantic import BaseModel, Field

from .serving import SCOPES, KeyIdentity, hash_secret, new_key, secret_matches

_COLLECTION = "api_keys"


class ApiKey(BaseModel):
    id: str
    name: str
    #: SHA-256 of the secret; the secret itself is never stored.
    secret_hash: str
    scopes: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    #: The key this one replaced.
    replaces: str | None = None


class ApiKeyOut(BaseModel):
    """A key as the studio shows it: everything but the hash."""

    id: str
    name: str
    scopes: list[str]
    publications: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    replaces: str | None = None
    #: How the key starts (``ndk_<id>_``), to recognise it.
    prefix: str


class NewApiKey(BaseModel):
    key: ApiKeyOut
    #: The complete key; shown this one time only.
    token: str


def _out(key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(**key.model_dump(exclude={"secret_hash"}), prefix=f"ndk_{key.id}_")


class ApiKeys:
    """The keys of this installation; every change reads and writes a key under one lock.

    Requests verify keys in worker threads while the studio revokes or rotates them: without the
    lock, a verification that read a key before it was revoked could store it back unrevoked.
    """

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    def _get(self, key_id: str) -> ApiKey:
        found = self.store.get(_COLLECTION, key_id, ApiKey)
        if found is None:
            raise KeyError(key_id)
        return found

    def all(self) -> list[ApiKeyOut]:
        return [_out(key) for key in sorted(self.store.list(_COLLECTION, ApiKey), key=lambda k: k.created_at)]

    def create(
        self, name: str, scopes: list[str], publications: list[str], *, replaces: str | None = None
    ) -> NewApiKey:
        name = name.strip()[:80]
        if not name:
            raise NodestroError("apikey.name_missing", "Give the key a name, e.g. the application that uses it")
        chosen = [scope for scope in SCOPES if scope in scopes]
        if not chosen:
            raise NodestroError("apikey.no_scope", "Choose at least one permission")
        if not publications:
            raise NodestroError("apikey.no_publication", "Choose at least one published workflow")
        for publication in publications:
            if self.store.get_raw("publications", publication) is None:
                raise KeyError(publication)
        key_id, secret, token = new_key()
        key = ApiKey(
            id=key_id,
            name=name,
            secret_hash=hash_secret(secret),
            scopes=chosen,
            publications=list(dict.fromkeys(publications)),
            replaces=replaces,
        )
        with self._lock:
            self.store.put(_COLLECTION, key.id, key)
        return NewApiKey(key=_out(key), token=token)

    def revoke(self, key_id: str) -> ApiKeyOut:
        with self._lock:
            key = self._get(key_id)
            if key.revoked_at is None:
                key.revoked_at = now()
                self.store.put(_COLLECTION, key.id, key)
        return _out(key)

    def rotate(self, key_id: str) -> NewApiKey:
        """A new key with the same name and permissions; the old one is revoked at once.

        Two rotations of the same key at the same time give one new key; the second answers
        ``apikey.revoked``. Should the process stop between the two writes, the old key is
        still valid (nothing is locked out) and the unseen new key can be revoked in the list.
        """
        with self._lock:
            old = self._get(key_id)
            if old.revoked_at is not None:
                raise NodestroError("apikey.revoked", "This key was revoked; create a new one")
            publications = [item for item in old.publications if self.store.get_raw("publications", item) is not None]
            created = self.create(old.name, old.scopes, publications, replaces=old.id)
            self.revoke(old.id)
        return created

    def delete(self, key_id: str) -> None:
        with self._lock:
            key = self._get(key_id)
            if key.revoked_at is None:
                raise NodestroError("apikey.delete_active", "Revoke the key before deleting it")
            self.store.delete(_COLLECTION, key_id)

    def forget_publication(self, publication_id: str) -> None:
        with self._lock:
            for key in self.store.list(_COLLECTION, ApiKey):
                if publication_id in key.publications:
                    key.publications = [item for item in key.publications if item != publication_id]
                    self.store.put(_COLLECTION, key.id, key)

    def verify(self, key_id: str, secret: str) -> KeyIdentity | None:
        key = self.store.get(_COLLECTION, key_id, ApiKey)
        if key is None or key.revoked_at is not None or not secret_matches(secret, key.secret_hash):
            return None
        moment = now()
        if key.last_used_at is None or moment - key.last_used_at > timedelta(minutes=1):
            with self._lock:
                # Read again: the key may have been revoked or changed since it was read above.
                key = self.store.get(_COLLECTION, key_id, ApiKey)
                if key is None or key.revoked_at is not None:
                    return None
                key.last_used_at = moment
                self.store.put(_COLLECTION, key.id, key)
        return KeyIdentity(id=key.id, name=key.name, scopes=key.scopes, publications=key.publications)
