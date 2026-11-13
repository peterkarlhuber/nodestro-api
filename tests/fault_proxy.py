"""A TCP proxy that loses one statement's reply, or cuts its connection while it runs, on purpose (JQ-4; the
same fault injection as nodestro-runtime's ``tests/postgres_support.py`` ``FaultProxy``, which is not part of the
runtime's package).

Kept in step with the runtime's copy: a change of the fault model belongs there first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress
import socket
import threading
from typing import Any

import postgres_support
import pytest


class FaultProxy:
    """A TCP proxy in front of the test server that loses one statement's traffic on purpose (JQ-1, JQ-3, RT2-03).

    :meth:`arm` names a statement by a part of its SQL text (as the client sends it: psycopg turns ``%s`` into
    ``$1``, ``$2`` …). The next client message that carries it (a simple ``Query`` or the ``Parse`` of the extended
    protocol) fires the fault once, on that connection only:

    - ``lose="reply"``: the statement goes to the server, which executes it (and, outside a transaction, commits it);
      every server message from then on, up to and including the ``ReadyForQuery`` that ends it, is dropped, and
      then both sides of the connection are closed. The client sees a lost connection after its statement ended.
    - ``lose="inflight"``: the statement (with the rest of the client's batch) goes to the server, and both sides
      are closed at once, without waiting for the server. The client sees a lost connection while the statement may
      still run; the server runs what it received and commits it, and its process ends when it next writes to the
      closed connection.
    - ``lose="request"``: the message is never forwarded; both sides are closed at once. The client sees a lost
      connection and the server never saw the statement.

    ``then`` runs once in the proxy, after the server ended the statement and before the client sees the cut
    (``reply``), or right after the cut (``inflight``, ``request``): a concurrent writer, or the release of a lock
    the statement waits for. :attr:`fired_backend` is the server process of the connection the fault fired on (from
    its ``BackendKeyData``).

    The proxy follows the protocol's message framing (``sslmode`` and ``gssencmode`` are disabled in :meth:`dsn`, so
    every server message is typed from the first byte), so the cut is placed by the protocol, never by a timer:
    nothing here sleeps or polls. Every other connection and statement passes through unchanged, and the client's
    new connections (a retry, the pool refilling) go through the proxy too.
    """

    MODES = ("reply", "inflight", "request")

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._armed: tuple[bytes, str, Callable[[], None] | None] | None = None
        #: How often a fault fired, and how often a lost reply was cut after the server ended the statement.
        self.fired = 0
        self.replies_lost = 0
        self.fired_backend: int | None = None
        #: An exception ``then`` raised (re-raised by the test through :meth:`check`).
        self.error: BaseException | None = None
        self._closing = False
        self._open: set[Any] = set()
        self._threads: list[threading.Thread] = []
        self._accepting = threading.Thread(target=self._accept, name="fault-proxy-accept", daemon=True)
        self._accepting.start()

    def dsn(self, base: str) -> str:
        """``base`` with this proxy as its server (no TLS, no GSS encryption: the proxy reads the plain protocol)."""
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        parts = conninfo_to_dict(base)
        parts.pop("hostaddr", None)
        parts.update(host="127.0.0.1", port=str(self.port), sslmode="disable", gssencmode="disable")
        return make_conninfo("", **parts)

    def arm(self, statement: bytes, *, lose: str = "reply", then: Callable[[], None] | None = None) -> None:
        """Fire once on the next client message whose SQL contains ``statement`` (see the class documentation)."""
        if lose not in self.MODES:
            raise ValueError(f"lose must be one of {self.MODES}, not {lose!r}")
        with self._lock:
            self._armed = (statement, lose, then)

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed is not None

    def check(self) -> None:
        """Re-raise what ``then`` raised."""
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        with self._lock:
            self._closing = True
            sockets = list(self._open)
        # Wake the blocked accept() portably: a connection to ourselves, which the accept loop sees and drops.
        with suppress(OSError):
            socket.create_connection(("127.0.0.1", self.port)).close()
        self._accepting.join(5)
        self._listener.close()
        for sock in sockets:
            self._cut(sock)
        for thread in self._threads:
            thread.join(5)

    # ---- the proxy -----------------------------------------------------------------------------

    def _accept(self) -> None:
        while True:
            try:
                client, _address = self._listener.accept()
            except OSError:
                return
            with self._lock:
                closing = self._closing
            if closing:
                client.close()
                return
            try:
                server = socket.create_connection(self._upstream)
            except OSError:
                client.close()
                continue
            link = _Link(client, server)
            with self._lock:
                self._open.update((client, server))
            for target, name in ((self._client_to_server, "up"), (self._server_to_client, "down")):
                thread = threading.Thread(target=target, args=(link,), name=f"fault-proxy-{name}", daemon=True)
                self._threads.append(thread)
                thread.start()

    def _take(self, link: _Link, kind: bytes, body: bytes) -> str | None:
        """The fault to fire for this client message, if it is the armed statement (disarms it)."""
        if kind not in (b"Q", b"P"):
            return None
        with self._lock:
            if self._armed is None or self._armed[0] not in body:
                return None
            _statement, lose, link.then = self._armed
            self._armed = None
            self.fired += 1
            self.fired_backend = link.backend
            return lose

    def _run_then(self, link: _Link) -> None:
        then, link.then = link.then, None
        if then is None:
            return
        try:
            then()
        except BaseException as exc:  # reported to the test by check()
            self.error = exc

    def _client_to_server(self, link: _Link) -> None:
        buffer = b""
        started = False  # the first message (startup, cancel request) has no type byte
        fault: str | None = None
        try:
            while True:
                data = link.client.recv(65536)
                if not data:
                    break
                buffer += data
                out = b""
                while True:
                    if not started:
                        if len(buffer) < 4:
                            break
                        size = int.from_bytes(buffer[:4], "big")
                        if len(buffer) < size:
                            break
                        out, buffer, started = out + buffer[:size], buffer[size:], True
                        continue
                    if len(buffer) < 5:
                        break
                    size = 1 + int.from_bytes(buffer[1:5], "big")
                    if len(buffer) < size:
                        break
                    message, buffer = buffer[:size], buffer[size:]
                    fault = self._take(link, message[:1], message[5:])
                    if fault == "request":
                        if out:
                            link.server.sendall(out)
                        return
                    if fault == "inflight":
                        link.server.sendall(out + message + buffer)  # the whole batch, through its Sync
                        return
                    if fault == "reply":
                        link.dropping.set()  # before the statement can reach the server
                    out += message
                if out:
                    link.server.sendall(out)
        except OSError:
            pass
        finally:
            self._close(link)
            if fault in ("request", "inflight"):
                self._run_then(link)

    def _server_to_client(self, link: _Link) -> None:
        buffer = b""
        try:
            while True:
                data = link.server.recv(65536)
                if not data:
                    break
                buffer += data
                out = b""
                while len(buffer) >= 5:
                    size = 1 + int.from_bytes(buffer[1:5], "big")
                    if len(buffer) < size:
                        break
                    message, buffer = buffer[:size], buffer[size:]
                    if message[:1] == b"K" and size >= 9:  # BackendKeyData: the server process id
                        link.backend = int.from_bytes(message[5:9], "big")
                    if not link.dropping.is_set():
                        out += message
                        continue
                    if message[:1] == b"Z":  # the server ended the statement: it ran (and committed) without us
                        if out:
                            link.client.sendall(out)
                        self._run_then(link)
                        with self._lock:
                            self.replies_lost += 1
                        return
                if out:
                    link.client.sendall(out)
        except OSError:
            pass
        finally:
            self._close(link)

    def _close(self, link: _Link) -> None:
        for sock in (link.client, link.server):
            self._cut(sock)
        with self._lock:
            self._open.difference_update((link.client, link.server))

    def _cut(self, sock: Any) -> None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            sock.close()


class _Link:
    """One proxied connection: the client's socket, the server's socket, whether its replies are being lost, its
    server process, and the ``then`` of a fault that fired on it."""

    def __init__(self, client: Any, server: Any) -> None:
        self.client = client
        self.server = server
        self.dropping = threading.Event()
        self.backend: int | None = None
        self.then: Callable[[], None] | None = None


def backend_gone(dsn: str, backend: int) -> bool:
    """Whether the server process ``backend`` has ended (one look, no waiting)."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        return connection.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (backend,)).fetchone() is None


@pytest.fixture
def fault_proxy() -> Iterator[FaultProxy]:
    """A :class:`FaultProxy` in front of ``NODESTRO_TEST_POSTGRES`` (skips without a test server)."""
    from psycopg.conninfo import conninfo_to_dict

    dsn = postgres_support.require_postgres()
    parts = conninfo_to_dict(dsn)
    proxy = FaultProxy(str(parts.get("hostaddr") or parts.get("host") or "127.0.0.1"), int(parts.get("port") or 5432))
    try:
        yield proxy
    finally:
        proxy.close()
