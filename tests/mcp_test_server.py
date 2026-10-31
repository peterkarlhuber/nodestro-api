"""A local MCP test server on the loopback interface (official SDK server) for the MCP API tests.

Nothing outside this process is contacted. The server runs uvicorn in a daemon thread on a free port.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import socket
import threading
import time
from typing import Any


@dataclass
class LoopbackServer:
    url: str
    mcp: Any
    stop: Callable[[], None]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_server() -> Any:
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    server = MCPServer("nodestro-api-test")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search(query: str) -> str:
        """Search the test catalog."""
        return f"results for {query}"

    @server.tool()
    def delete_page(page_id: str) -> str:
        """Delete a page."""
        return f"deleted {page_id}"

    return server


def serve(server: Any | None = None) -> LoopbackServer:
    import uvicorn

    server = server or build_server()
    port = _free_port()
    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error", lifespan="on")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("test server did not start")
        time.sleep(0.02)

    def stop() -> None:
        runner.should_exit = True
        thread.join(timeout=5)

    return LoopbackServer(url=f"http://127.0.0.1:{port}/mcp", mcp=server, stop=stop)
