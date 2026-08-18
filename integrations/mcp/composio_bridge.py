"""
integrations/mcp/composio_bridge.py — Composio as an MCP provider
===================================================================
Composio hosts MCP-compatible servers per "toolkit" (their name for an
app integration: gmail, googlecalendar, slack, notion, github, spotify,
home_assistant, etc). Rather than hardcode Composio's exact endpoint
URL shape here (SaaS APIs like this move fast and Gama's contributors
will read this file long after today), this module reads the concrete
per-toolkit URLs from config — see config/mcp_servers.example.json —
and just adapts them into McpServerConnection objects.

Two ways to populate that config:

1. Manual (works today, no extra dependency):
   Log into the Composio dashboard, connect each toolkit you want
   (Google Calendar, Gmail, Slack, ...), and copy the MCP server URL
   Composio gives you for that toolkit + your API key into
   config/mcp_servers.json. This is the safest path because it doesn't
   depend on any particular version of a `composio` SDK.

2. Programmatic (optional):
   If the `composio` pip package is installed and COMPOSIO_API_KEY is
   set, `discover_composio_toolkits()` will call it to auto-list which
   toolkits you've connected and their MCP URLs, so you don't have to
   hand-copy them. This is best-effort: if the installed SDK's API
   doesn't match what we expect, we log a warning and fall back to
   whatever is already in mcp_servers.json.

Author: Gama MCP integration layer
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from integrations.mcp.mcp_client import McpServerConnection
from utils.logger import get_logger

log = get_logger(__name__)


def build_composio_connection(
    toolkit: str,
    url: str,
    api_key: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> McpServerConnection:
    """
    Build one McpServerConnection for a single Composio toolkit MCP URL.

    `url` is whatever Composio's dashboard gave you for this toolkit —
    typically an https:// streamable-HTTP MCP endpoint that already
    encodes your connected-account/toolkit selection.
    """
    headers = dict(extra_headers or {})
    if api_key:
        # Composio's documented auth header name has varied across SDK
        # versions (x-api-key vs Authorization: Bearer). Send both so
        # this keeps working regardless of which one the current
        # Composio backend expects; harmless extra header otherwise.
        headers.setdefault("x-api-key", api_key)
        headers.setdefault("Authorization", f"Bearer {api_key}")

    return McpServerConnection(
        name=f"composio_{toolkit}",
        transport="http",
        url=url,
        headers=headers,
    )


def discover_composio_toolkits(api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Best-effort auto-discovery of connected Composio toolkits and their
    MCP URLs, using the `composio` SDK if installed.

    Returns a list of dicts like: {"toolkit": "googlecalendar", "url": "..."}.
    Returns [] (never raises) if the SDK isn't installed or its shape
    doesn't match what we expect — callers should treat that as "fall
    back to manual config", not as a hard failure.
    """
    api_key = api_key or os.environ.get("COMPOSIO_API_KEY", "")
    if not api_key:
        log.info("composio: no API key set, skipping auto-discovery")
        return []

    try:
        import composio  # type: ignore
    except ImportError:
        log.info(
            "composio: `composio` package not installed — add toolkit "
            "MCP URLs manually in config/mcp_servers.json instead"
        )
        return []

    try:
        # NOTE: the composio SDK's client surface has changed across
        # versions; this call is intentionally guarded rather than
        # assumed. If this breaks against whatever version you have
        # installed, check Composio's current docs and adjust — the
        # rest of the MCP pipeline (mcp_client.py, registry_bridge.py)
        # does not depend on this function working.
        client = composio.Composio(api_key=api_key)  # type: ignore[attr-defined]
        toolkits = client.mcp.list()  # type: ignore[attr-defined]
        out = []
        for tk in toolkits:
            out.append({
                "toolkit": getattr(tk, "toolkit", getattr(tk, "app", "unknown")),
                "url": getattr(tk, "url", getattr(tk, "server_url", "")),
            })
        return [t for t in out if t["url"]]
    except Exception as exc:
        log.warning(
            "composio: auto-discovery failed (%s) — falling back to "
            "manual config/mcp_servers.json entries", exc
        )
        return []
