"""
actions/protocol_engine.py — Refactored First-Class JARVIS Protocol Engine Tool
================================================================================
Full-featured tool endpoint for Gama's first-class Protocol System.
Delegates to core.protocols.manager.protocol_manager for modular storage,
intelligent execution, editing, search, duplication, import/export, and controls.
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
from typing import Any, Dict, Optional

from core.protocols.manager import protocol_manager
from core.protocols.registry import protocol_registry, normalize_identifier

log = get_logger(__name__)
logger = log  # back-compat alias
def normalize_protocol_id(identifier: str) -> str:
    """Helper backwards compatibility for fast_intent router."""
    num_id, slug = normalize_identifier(identifier)
    if num_id is not None:
        return f"protocol {num_id}"
    return f"protocol {slug.replace('_', ' ')}" if slug else ""


def protocol_engine(action: str = "list", **kwargs) -> str:
    """Unified entry point for JARVIS-style numbered/named Protocols.

    Actions:
      create    - identifier ('17', 'Coding Protocol'), steps ("open Chrome, then open Spotify"), description
      run       - identifier ('17', 'Coding Protocol'), parameters (dict or "project=Gama")
      delete    - identifier
      list      - (no args) or category
      search    - query
      rename    - identifier, new_name
      duplicate - identifier, new_identifier
      export    - filepath (optional)
      import    - data_or_filepath
      pause     - (no args or execution_id)
      resume    - (no args or execution_id)
      cancel    - (no args or execution_id)
      skip      - (no args or execution_id)
      status    - (no args or identifier)
    """
    action = (action or "list").strip().lower()
    identifier = (
        kwargs.get("identifier") or kwargs.get("name")
        or kwargs.get("id") or kwargs.get("number") or ""
    )

    if action == "list":
        category = kwargs.get("category")
        protocols = protocol_manager.list_protocols(category=category)
        if not protocols:
            return "No protocols configured yet. Say 'create Protocol 17' or 'Create Coding Protocol' to make one."
        items = []
        for p in protocols:
            num_str = f" (#{p.numeric_id})" if p.numeric_id else ""
            step_summary = ", ".join(f"{s.action_type}:{s.target}" for s in p.steps[:3])
            if len(p.steps) > 3:
                step_summary += f" (+{len(p.steps) - 3} more)"
            items.append(f"• {p.display_name}{num_str} [{len(p.steps)} steps: {step_summary}]")
        return "Configured Protocols:\n" + "\n".join(items)

    if action in ("search", "find"):
        query = kwargs.get("query") or kwargs.get("text") or identifier
        results = protocol_manager.search_protocols(query)
        if not results:
            return f"No protocols found matching '{query}', Sir."
        items = [f"• {p.display_name} ({len(p.steps)} steps)" for p in results]
        return f"Found {len(results)} protocol(s) for '{query}':\n" + "\n".join(items)

    if action in ("create", "add", "make", "define", "set"):
        steps_text = kwargs.get("steps") or kwargs.get("text") or kwargs.get("actions") or ""
        if not identifier:
            return "Which protocol? Give it a name or number, e.g. 'Protocol 17' or 'Coding Protocol'."
        if not steps_text:
            return f"What should Protocol '{identifier}' actually do, Sir? Describe the steps."

        ok, msg, _ = protocol_manager.create_protocol(
            identifier=identifier,
            steps_text=steps_text,
            description=kwargs.get("description", ""),
            category=kwargs.get("category", "General"),
            confirmation_required=bool(kwargs.get("confirmation_required", False)),
        )
        return msg

    if action in ("run", "execute", "start", "activate", "engage", "initiate"):
        if not identifier:
            return "Which protocol should I run, Sir?"
        
        # Parse parameters if provided (e.g., "for Gama" -> {"param": "Gama"})
        raw_params = kwargs.get("parameters") or kwargs.get("params") or {}
        params: Dict[str, Any] = {}
        if isinstance(raw_params, dict):
            params = raw_params
        elif isinstance(raw_params, str) and raw_params:
            params = {"target": raw_params}

        ok, msg = protocol_manager.execute_protocol(identifier, parameters=params)
        return msg

    if action in ("delete", "remove"):
        if not identifier:
            return "Which protocol should I delete, Sir?"
        ok, msg = protocol_manager.delete_protocol(identifier)
        return msg

    if action == "rename":
        new_name = kwargs.get("new_name") or kwargs.get("to") or ""
        if not identifier or not new_name:
            return "Usage: rename protocol [old_name] to [new_name]."
        ok, msg = protocol_manager.rename_protocol(identifier, new_name)
        return msg

    if action == "duplicate":
        new_id = kwargs.get("new_identifier") or kwargs.get("to") or f"{identifier}_copy"
        ok, msg, _ = protocol_manager.duplicate_protocol(identifier, new_id)
        return msg

    if action == "export":
        filepath = kwargs.get("filepath")
        return protocol_manager.export_protocols(filepath)

    if action == "import":
        data = kwargs.get("data") or kwargs.get("filepath") or ""
        ok, msg = protocol_manager.import_protocols(data)
        return msg

    if action == "pause":
        protocol_manager.pause_protocol()
        return "Protocol execution paused, Sir."

    if action == "resume":
        protocol_manager.resume_protocol()
        return "Resuming protocol execution, Sir."

    if action == "cancel":
        protocol_manager.cancel_protocol()
        return "Protocol execution cancelled, Sir."

    if action == "skip":
        protocol_manager.skip_step()
        return "Skipping current protocol step, Sir."

    if action == "status":
        history = protocol_manager.get_history(limit=5)
        if not history:
            return "No recent protocol execution history."
        last = history[0]
        return f"Last Protocol Run: {last.protocol_name} [{last.status}] — Started {int(last.started_at)}."

    return f"Unknown protocol action '{action}'. Supported actions: create, run, delete, list, search, rename, duplicate, export, import, pause, resume, cancel, skip, status."


__all__ = ["protocol_engine", "normalize_protocol_id"]
