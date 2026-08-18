"""
core/tool_registry.py — Tool Registry & Capability Manager
===========================================================
Replaces the 300-line if-elif chain in _execute_tool_impl with an O(1)
dict dispatch table.  Each tool is registered once with its handler
callable and risk level.  The registry is populated at startup from
main.py via ``register_all_tools()``.

Design goals
------------
• O(1) dispatch — no sequential if-elif scan
• Runtime iteration — health checks, docs, listing available tools
• Per-tool risk metadata — consumed by CapabilityManager / ConfidenceScorer
• Lazy-safe — handlers are already-resolved callables (lazy imports in
  main.py fire before register_all_tools is called)
• No circular imports — this module imports only from core/confidence.py
  and utils; main.py imports this module and provides the handlers

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.confidence import ActionRisk
from utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolEntry:
    """Metadata + handler for one registered tool."""
    name: str
    handler: Callable[[Dict[str, Any]], str]
    risk: ActionRisk = ActionRisk.LOW
    description: str = ""
    # Optional post-execution verifier: called with (name, args, result)
    # Returns (ok: bool, detail: str).  None = trust the result string.
    verifier: Optional[Callable[[str, dict, str], tuple]] = None
    # If True this tool can be retried on transient failure
    retryable: bool = True
    # Human-readable category for health reports
    category: str = "general"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    Central registry for all GAMA tool handlers.

    Usage (in main.py)::

        from core.tool_registry import tool_registry

        tool_registry.register(
            "open_app",
            handler=lambda args: open_app(args.get("app_name", "")),
            risk=ActionRisk.LOW,
            description="Launch a desktop application.",
        )

        # Dispatch (replaces _execute_tool_impl):
        result = tool_registry.dispatch("open_app", {"app_name": "notepad"})
    """

    def __init__(self) -> None:
        self._entries: Dict[str, ToolEntry] = {}
        self._lock = threading.RLock()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        handler: Callable[[Dict[str, Any]], str],
        *,
        risk: ActionRisk = ActionRisk.LOW,
        description: str = "",
        verifier: Optional[Callable[[str, dict, str], tuple]] = None,
        retryable: bool = True,
        category: str = "general",
    ) -> None:
        """Register a tool handler. Overwrites any previous registration."""
        with self._lock:
            self._entries[name] = ToolEntry(
                name=name,
                handler=handler,
                risk=risk,
                description=description,
                verifier=verifier,
                retryable=retryable,
                category=category,
            )

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def dispatch(self, name: str, args: dict) -> str:
        """
        Route a tool call to its registered handler.

        Returns the handler's result string, or an ``Unknown tool`` /
        ``Tool failed`` message on error.  Never raises.
        """
        entry = self._get(name)
        if entry is None:
            log.warning(f"[ToolRegistry] No handler registered for tool: {name!r}")
            return f"Unknown tool: {name}"
        try:
            return entry.handler(args)
        except Exception as exc:
            log.error(f"[ToolRegistry] Handler for {name!r} raised: {exc}", exc_info=True)
            return f"Tool failed: {exc}"

    # ── Queries ───────────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._entries

    def risk_of(self, name: str) -> ActionRisk:
        """Return the risk level for a tool, defaulting to LOW if unknown."""
        entry = self._get(name)
        return entry.risk if entry else ActionRisk.LOW

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        return self._get(name)

    def list_tools(self) -> List[ToolEntry]:
        """Return all registered tools (snapshot, thread-safe)."""
        with self._lock:
            return list(self._entries.values())

    def list_names(self) -> List[str]:
        with self._lock:
            return list(self._entries.keys())

    def by_risk(self, risk: ActionRisk) -> List[ToolEntry]:
        """Return all tools at a given risk level."""
        with self._lock:
            return [e for e in self._entries.values() if e.risk == risk]

    def health_summary(self) -> Dict[str, int]:
        """Count of registered tools per risk level (for health reporting)."""
        summary: Dict[str, int] = {}
        with self._lock:
            for e in self._entries.values():
                summary[e.risk.value] = summary.get(e.risk.value, 0) + 1
        return summary

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get(self, name: str) -> Optional[ToolEntry]:
        with self._lock:
            return self._entries.get(name)


# Process-wide singleton
tool_registry = ToolRegistry()


__all__ = [
    "ToolEntry",
    "ToolRegistry",
    "tool_registry",
]
