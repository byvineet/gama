"""
integrations/mcp/registry_bridge.py — MCP tools -> Gama's ToolRegistry
=========================================================================
This is the ONLY file that touches Gama's existing tool infrastructure.
Everything upstream of it (mcp_client.py, composio_bridge.py) knows
nothing about ToolRegistry, ActionRisk, or Gemini function-calling
schemas — that keeps the tool layer provider-agnostic, per the
redesign brief: swapping or adding MCP servers never requires touching
core/tool_dispatch.py, core/capability_manager.py, or main.py beyond
the one bootstrap call below.

What this module does, at startup:

  1. Read config/mcp_servers.json.
  2. For each *enabled* server entry, open a connection (direct stdio,
     or a Composio-hosted toolkit over HTTP).
  3. list_tools() on each connection.
  4. Register each tool into `core.tool_registry.tool_registry` with a
     namespaced name (e.g. "gcal_create_event"), a risk tier derived
     from the entry's config (default MEDIUM — MCP tools default to a
     stricter tier than local SAFE/LOW actions until proven otherwise),
     and a handler that just calls back into the MCP connection.
  5. Convert each tool's JSON-schema input into a Gemini function
     declaration and expose it via `get_mcp_tool_declarations()` so
     main.py's `_select_tool_declarations()` can merge it in exactly
     like the static ones in core/tool_declarations.py.

Wire-in (one line, same pattern as core/jarvis_bootstrap.py)
--------------------------------------------------------------
    from integrations.mcp.registry_bridge import bootstrap_mcp_tools
    bootstrap_mcp_tools()      # call once, near GamaAssistant.__init__

Author: Gama MCP integration layer
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.confidence import ActionRisk
from core.tool_registry import tool_registry
from integrations.mcp.composio_bridge import build_composio_connection
from integrations.mcp.mcp_client import McpServerConnection, McpToolSpec
from utils.logger import get_logger
from utils.paths import get_base_dir  # existing helper used elsewhere in the project

log = get_logger(__name__)

_CONFIG_PATH = Path(get_base_dir()) / "config" / "mcp_servers.json"

_lock = threading.Lock()
_bootstrapped = False
_connections: Dict[str, McpServerConnection] = {}
_mcp_declarations: List[Dict[str, Any]] = []


# ---------------------------------------------------------------------------
# JSON-schema (MCP) -> Gemini function-declaration parameter schema
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}


def _convert_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort JSON-Schema -> Gemini declaration schema conversion."""
    if not schema:
        return {"type": "OBJECT", "properties": {}}
    out: Dict[str, Any] = {"type": _TYPE_MAP.get(schema.get("type", "object"), "OBJECT")}
    if "description" in schema:
        out["description"] = schema["description"]
    if schema.get("type") == "object" or "properties" in schema:
        props = {}
        for key, val in (schema.get("properties") or {}).items():
            props[key] = _convert_schema(val) if isinstance(val, dict) else {"type": "STRING"}
        out["properties"] = props
        if schema.get("required"):
            out["required"] = schema["required"]
    if schema.get("type") == "array" and "items" in schema:
        out["items"] = _convert_schema(schema["items"])
    return out


def _risk_for(entry_cfg: Dict[str, Any]) -> ActionRisk:
    raw = (entry_cfg.get("default_risk") or "medium").lower()
    try:
        return ActionRisk(raw)
    except ValueError:
        log.warning("mcp config: unknown risk '%s', defaulting to MEDIUM", raw)
        return ActionRisk.MEDIUM


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> List[Dict[str, Any]]:
    if not _CONFIG_PATH.exists():
        log.info("mcp: no config/mcp_servers.json found — no MCP tools registered")
        return []
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("mcp: failed to parse %s: %s", _CONFIG_PATH, exc)
        return []
    servers = data.get("servers", [])
    return [s for s in servers if s.get("enabled", True)]


def _build_connection(entry: Dict[str, Any]) -> Optional[McpServerConnection]:
    kind = entry.get("kind", "stdio")
    name = entry.get("name") or entry.get("toolkit") or "unnamed"
    try:
        if kind == "composio":
            return build_composio_connection(
                toolkit=entry["toolkit"],
                url=entry["url"],
                api_key=entry.get("api_key"),
            )
        if kind == "http":
            return McpServerConnection(
                name=name, transport="http", url=entry["url"],
                headers=entry.get("headers") or {},
            )
        # default: local stdio server, e.g. `npx @modelcontextprotocol/server-github`
        return McpServerConnection(
            name=name,
            transport="stdio",
            command=entry["command"],
            args=entry.get("args", []),
            env=entry.get("env"),
        )
    except KeyError as exc:
        log.warning("mcp: server entry '%s' missing required field %s — skipping", name, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bootstrap_mcp_tools(config_path: Optional[Path] = None) -> int:
    """
    Connect every enabled server in config/mcp_servers.json and register
    its tools into Gama's existing ToolRegistry. Idempotent — safe to
    call more than once (subsequent calls are no-ops).

    Returns the number of MCP tools successfully registered.
    """
    global _bootstrapped
    with _lock:
        if _bootstrapped:
            return len(_mcp_declarations)

        path = config_path or _CONFIG_PATH
        entries = _load_config() if path == _CONFIG_PATH else json.loads(
            path.read_text(encoding="utf-8")
        ).get("servers", [])

        total = 0
        for entry in entries:
            name = entry.get("name") or entry.get("toolkit") or "unnamed"
            conn = _build_connection(entry)
            if conn is None:
                continue
            try:
                conn.connect(timeout=entry.get("connect_timeout", 15.0))
            except Exception as exc:
                log.warning(
                    "mcp[%s]: could not connect (%s) — this server's tools "
                    "will be unavailable this session, everything else "
                    "continues normally", name, exc,
                )
                continue

            try:
                specs = conn.list_tools()
            except Exception as exc:
                log.warning("mcp[%s]: list_tools() failed: %s", name, exc)
                conn.close()
                continue

            _connections[name] = conn
            risk = _risk_for(entry)
            allow = set(entry.get("allow_tools", []) or [])
            deny = set(entry.get("deny_tools", []) or [])

            for spec in specs:
                if allow and spec.name not in allow:
                    continue
                if spec.name in deny:
                    continue
                _register_one(conn, spec, risk, category=entry.get("category", "mcp"))
                total += 1

        _bootstrapped = True
        log.info("mcp: registered %d tool(s) across %d server(s)", total, len(_connections))
        return total


def _register_one(conn: McpServerConnection, spec: McpToolSpec, risk: ActionRisk, category: str) -> None:
    qualified = spec.qualified_name

    def _handler(args: Dict[str, Any], _conn=conn, _raw_name=spec.name) -> str:
        return _conn.call_tool(_raw_name, args)

    tool_registry.register(
        qualified,
        handler=_handler,
        risk=risk,
        description=spec.description,
        retryable=True,
        category=category,
    )
    _mcp_declarations.append({
        "name": qualified,
        "description": spec.description or f"MCP tool '{spec.name}' from {spec.server_name}.",
        "parameters": _convert_schema(spec.input_schema),
    })


def get_mcp_tool_declarations() -> List[Dict[str, Any]]:
    """Gemini function-declaration dicts for every registered MCP tool.

    Merge into the static list from core/tool_declarations.py, e.g. in
    GamaAssistant._select_tool_declarations():

        from integrations.mcp.registry_bridge import get_mcp_tool_declarations
        decls = TOOL_DECLARATIONS + get_mcp_tool_declarations()
    """
    return list(_mcp_declarations)


def shutdown_mcp_tools() -> None:
    """Close all MCP server connections cleanly (call on app exit)."""
    global _bootstrapped
    with _lock:
        for conn in _connections.values():
            try:
                conn.close()
            except Exception:
                pass
        _connections.clear()
        _mcp_declarations.clear()
        _bootstrapped = False
