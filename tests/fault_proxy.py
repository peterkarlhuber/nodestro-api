"""A TCP proxy that loses one statement's reply on purpose (JQ-4; the same fault injection as nodestro-runtime's
``tests/postgres_support.py`` ``FaultProxy``, which is not part of the runtime's package).

Kept in step with the runtime's copy: a change of the fault model belongs there first.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
import os
import socket
import threading
from typing import Any

import pytest


class FaultProxy:
    """A TCP proxy in front of the test server that loses one statement's traffic on purpose (JQ-1, RT2-03).

    :meth:`arm` names a statement by a part of its SQL text (as the client sends it: psycopg turns ``%s`` into
    ``$1``, ``$2`` …). The next client message that carries it (a simple ``Query`` or the ``Parse`` of the extended
    protocol) fires the fault once, on that connection only:

    - ``lose="reply"``: the statement goes to the server, which executes it (and, outside a transaction, commits it);
      every server message from then on, up to and including the ``ReadyForQuery`` that ends the statement, is
      dropped, and then both sides of the connection are closed. The client sees a lost connection after its
      statement was applied.
    - ``lose="request"``: the message is never forwarded; both sides are closed at once. The client sees a lost
      connection and the server never saw the statement.

    The proxy follows the protocol's message framing (``sslmode`` and ``gssencmode`` are disabled in :meth:`dsn`, so
    every server message is typed from the first byte), so the cut is placed by the server's own end-of-statement
    message and never by a timer: nothing here sleeps or polls. Every other connection and statement passes through
    unchanged, and the client's new connections (a retry, the pool refilling) go through the proxy too.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._armed: tuple[bytes, str] | None = None
        #: How often a fault fired, and how often a lost reply was cut after the server ended the statement.
        self.fired = 0
        self.replies_lost = 0
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

    def arm(self, statement: bytes, *, lose: str = "reply") -> None:
        """Fire once on the next client message whose SQL contains ``statement`` (see the class documentation)."""
        if lose not in ("reply", "request"):
            raise ValueError(f"lose must be 'reply' or 'request', not {lose!r}")
        with self._lock:
            self._armed = (statement, lose)

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed is not None

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

    def _take(self, kind: bytes, body: bytes) -> str | None:
        """The fault to fire for this client message, if it is the armed statement (disarms it)."""
        if kind not in (b"Q", b"P"):
            return None
        with self._lock:
            if self._armed is None or self._armed[0] not in body:
                return None
            lose = self._armed[1]
            self._armed = None
            self.fired += 1
            return lose

    def _client_to_server(self, link: _Link) -> None:
        buffer = b""
        started = False  # the first message (startup, cancel request) has no type byte
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
                    fault = self._take(message[:1], message[5:])
                    if fault == "request":
                        if out:
                            link.server.sendall(out)
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
                    if not link.dropping.is_set():
                        out += message
                        continue
                    if message[:1] == b"Z":  # the server ended the statement: it ran (and committed) without us
                        if out:
                            link.client.sendall(out)
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
    """One proxied connection: the client's socket, the server's socket, and whether its replies are being lost."""

    def __init__(self, client: Any, server: Any) -> None:
        self.client = client
        self.server = server
        self.dropping = threading.Event()


@pytest.fixture
def fault_proxy() -> Iterator[FaultProxy]:
    """A :class:`FaultProxy` in front of ``NODESTRO_TEST_POSTGRES`` (skips without a test server)."""
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get("NODESTRO_TEST_POSTGRES", "")
    if not dsn:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    parts = conninfo_to_dict(dsn)
    proxy = FaultProxy(str(parts.get("hostaddr") or parts.get("host") or "127.0.0.1"), int(parts.get("port") or 5432))
    try:
        yield proxy
    finally:
        proxy.close()
