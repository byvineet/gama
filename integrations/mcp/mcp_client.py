"""
integrations/mcp/mcp_client.py — Generic MCP client for Gama
==============================================================
Gama's tool handlers are synchronous (`Callable[[dict], str]`, see
core/tool_registry.py). The official MCP Python SDK is asyncio-only.
Rather than sprinkle `asyncio.run()` everywhere (which breaks if a
handler is ever called from inside another event loop, and re-opens a
process/connection on every single call), this module runs ONE
background event loop per server connection, in its own thread,
and exposes plain synchronous methods on top of it.

Two transports are supported, matching what real MCP servers use:
  - stdio   : spawn a local process (e.g. `npx @modelcontextprotocol/server-github`)
  - http    : streamable-HTTP endpoint (this is what Composio's hosted
              per-toolkit MCP servers speak — see composio_bridge.py)

Usage
-----
    server = McpServerConnection(
        name="github",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "..."},
    )
    server.connect(timeout=15)
    tools = server.list_tools()                 # -> list[McpToolSpec]
    result = server.call_tool("create_issue", {"title": "..."})   # -> str
    server.close()

Author: Gama MCP integration layer
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

# The `mcp` package is the official Model Context Protocol Python SDK.
# pip install mcp
try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    _HAS_STDIO = True
except Exception:  # pragma: no cover - optional dependency
    ClientSession = None  # type: ignore
    stdio_client = None  # type: ignore
    StdioServerParameters = None  # type: ignore
    _HAS_STDIO = False

try:
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    _HAS_HTTP = True
except Exception:  # pragma: no cover - optional dependency
    httpx = None  # type: ignore
    streamable_http_client = None  # type: ignore
    _HAS_HTTP = False


@dataclass
class McpToolSpec:
    """One tool as advertised by an MCP server, normalized for Gama."""
    name: str                      # raw MCP tool name, e.g. "create_event"
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)  # JSON schema
    server_name: str = ""          # e.g. "google_calendar"

    @property
    def qualified_name(self) -> str:
        """Namespaced name Gama registers with, e.g. 'gcal_create_event'."""
        prefix = self.server_name.strip().lower().replace(" ", "_")
        return f"{prefix}_{self.name}" if prefix else self.name


class McpConnectionError(RuntimeError):
    pass


class _LoopThread:
    """One background thread running one asyncio event loop forever."""

    def __init__(self, name: str):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name=f"mcp-loop-{name}", daemon=True
        )
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro, timeout: Optional[float] = None):
        """Schedule a coroutine on the background loop and block for its result."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)


class McpServerConnection:
    """
    A single long-lived connection to one MCP server.

    Kept deliberately dumb: connect / list_tools / call_tool / close.
    All the "which tools should exist and what do they map to in Gama"
    logic lives in registry_bridge.py, not here — this class doesn't
    know anything about ToolRegistry, risk levels, or Gemini schemas.
    """

    def __init__(
        self,
        name: str,
        transport: str,                      # "stdio" | "http"
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        call_timeout: float = 20.0,
    ):
        if transport not in ("stdio", "http"):
            raise ValueError(f"unknown transport '{transport}'")
        self.name = name
        self.transport = transport
        self.command = command
        self.args = args or []
        self.env = env or None
        self.url = url
        self.headers = headers or {}
        self.call_timeout = call_timeout

        self._loop_thread: Optional[_LoopThread] = None
        self._session: Optional["ClientSession"] = None
        self._ctx_stack: List[Any] = []
        self._connected = False
        self._lock = threading.RLock()

    # -- lifecycle ----------------------------------------------------

    def connect(self, timeout: float = 15.0) -> None:
        with self._lock:
            if self._connected:
                return
            if self.transport == "stdio" and not _HAS_STDIO:
                raise McpConnectionError(
                    "mcp package not installed — `pip install mcp`"
                )
            if self.transport == "http" and not _HAS_HTTP:
                raise McpConnectionError(
                    "mcp streamable-http client not available — "
                    "`pip install mcp[streamablehttp]` or upgrade `mcp`"
                )

            self._loop_thread = _LoopThread(self.name)
            try:
                self._loop_thread.run(self._async_connect(), timeout=timeout)
                self._connected = True
                log.info("mcp[%s]: connected via %s", self.name, self.transport)
            except Exception:
                self._loop_thread.stop()
                self._loop_thread = None
                raise

    async def _async_connect(self):
        if self.transport == "stdio":
            params = StdioServerParameters(
                command=self.command, args=self.args, env=self.env
            )
            cm = stdio_client(params)
        else:
            # Streamable-HTTP takes headers/auth via a pre-built httpx
            # client, not a `headers=` kwarg on the transport itself.
            http_client = httpx.AsyncClient(headers=self.headers) if self.headers else None
            cm = streamable_http_client(self.url, http_client=http_client)

        # Enter the transport context manager manually so it stays open
        # for the lifetime of this connection (this loop never returns
        # control to the caller until close() tears it down).
        entered = await cm.__aenter__()
        self._ctx_stack.append(cm)
        read, write = entered[0], entered[1]

        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        self._ctx_stack.append(session_cm)
        await session.initialize()
        self._session = session

    def close(self) -> None:
        with self._lock:
            if not self._connected or not self._loop_thread:
                return
            try:
                self._loop_thread.run(self._async_close(), timeout=10)
            except Exception as exc:
                log.warning("mcp[%s]: error during close: %s", self.name, exc)
            finally:
                self._loop_thread.stop()
                self._loop_thread = None
                self._connected = False

    async def _async_close(self):
        for cm in reversed(self._ctx_stack):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._ctx_stack.clear()
        self._session = None

    # -- tool discovery / invocation ------------------------------------

    def list_tools(self) -> List[McpToolSpec]:
        if not self._connected:
            self.connect()
        result = self._loop_thread.run(self._session.list_tools(), timeout=self.call_timeout)
        specs = []
        for t in result.tools:
            # Field name has varied across mcp SDK versions
            # (inputSchema in 1.x, input_schema in 2.x) — accept either.
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
            specs.append(
                McpToolSpec(
                    name=t.name,
                    description=t.description or "",
                    input_schema=schema,
                    server_name=self.name,
                )
            )
        return specs

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Synchronous call — safe to use directly as a Gama tool handler body."""
        if not self._connected:
            self.connect()
        try:
            result = self._loop_thread.run(
                self._session.call_tool(tool_name, arguments),
                timeout=self.call_timeout,
            )
        except TimeoutError:
            return f"[{self.name}] timed out after {self.call_timeout:.0f}s calling '{tool_name}'."
        except Exception as exc:
            log.warning("mcp[%s]: call_tool(%s) failed: %s", self.name, tool_name, exc)
            return f"[{self.name}] error calling '{tool_name}': {exc}"

        if getattr(result, "isError", False):
            text = _flatten_content(result.content)
            return f"[{self.name}] tool '{tool_name}' reported an error: {text}"
        return _flatten_content(result.content)


def _flatten_content(content) -> str:
    """MCP tool results are a list of content blocks (text/image/etc)."""
    if not content:
        return ""
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts).strip()
