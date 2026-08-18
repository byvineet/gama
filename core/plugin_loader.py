"""
core/plugin_loader.py — Drop-in plugin / extension system
=========================================================
Phase 2: add a new tool without editing tool_dispatch.py or
tool_declarations.py.

Place a Python file in ``plugins/`` that exposes:

    PLUGIN = {
        "name": "my_tool",
        "description": "What this tool does (shown to Gemini).",
        "parameters": { ... JSON-schema-like object ... },  # optional
        "risk": "LOW",          # SAFE | LOW | MEDIUM | HIGH | CRITICAL
        "category": "general",
        "behavior": "NON_BLOCKING",  # or BLOCKING
        "handler": callable,    # def handler(args: dict) -> str
    }

Or a list of such dicts under PLUGIN / PLUGINS.

At startup ``load_plugins()`` imports every ``*.py`` (except ``_*``),
registers handlers with ToolRegistry, and returns declaration fragments
for Gemini Live tool lists.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_loaded: List[Dict[str, Any]] = []
_loaded_names: set[str] = set()


def _plugins_dir() -> Path:
    try:
        from utils.paths import get_base_dir
        return get_base_dir() / "plugins"
    except Exception:
        return Path(__file__).resolve().parent.parent / "plugins"


def _risk_from_str(value: Any):
    from core.confidence import ActionRisk
    if isinstance(value, ActionRisk):
        return value
    name = str(value or "LOW").upper().strip()
    return getattr(ActionRisk, name, ActionRisk.LOW)


def _normalize_entry(raw: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = (raw.get("name") or "").strip()
    handler = raw.get("handler")
    if not name or not callable(handler):
        log.warning(f"[plugins] skip invalid entry in {source}: need name + handler")
        return None
    params = raw.get("parameters") or {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    }
    return {
        "name": name,
        "handler": handler,
        "description": (raw.get("description") or f"Plugin tool {name}").strip(),
        "parameters": params,
        "risk": _risk_from_str(raw.get("risk", "LOW")),
        "category": (raw.get("category") or "plugin").strip(),
        "behavior": (raw.get("behavior") or "NON_BLOCKING").strip().upper(),
        "source": source,
    }


def _iter_plugin_dicts(module: Any, source: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for attr in ("PLUGIN", "PLUGINS", "plugin", "plugins"):
        val = getattr(module, attr, None)
        if val is None:
            continue
        if isinstance(val, dict) and "handler" in val:
            entry = _normalize_entry(val, source)
            if entry:
                out.append(entry)
        elif isinstance(val, (list, tuple)):
            for item in val:
                entry = _normalize_entry(item, source)
                if entry:
                    out.append(entry)
    # Convention: module-level register(registry) hook
    return out


def _load_module_from_path(path: Path) -> Optional[Any]:
    mod_name = f"gama_plugins.{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        log.warning(f"[plugins] failed to import {path.name}: {exc}")
        return None


def load_plugins(register: bool = True) -> List[Dict[str, Any]]:
    """Discover plugins/, optionally register with ToolRegistry.

    Returns the list of normalized plugin entries (including already-loaded).
    Safe to call more than once; re-scans and skips duplicate tool names.
    """
    global _loaded
    pdir = _plugins_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    # Ensure plugins is a package for relative imports inside plugins
    init = pdir / "__init__.py"
    if not init.exists():
        try:
            init.write_text('"""GAMA drop-in plugins."""\n', encoding="utf-8")
        except Exception:
            pass

    discovered: List[Dict[str, Any]] = []
    for path in sorted(pdir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _load_module_from_path(path)
        if module is None:
            continue
        entries = _iter_plugin_dicts(module, path.name)
        # Optional: module.register(tool_registry) for advanced plugins
        if hasattr(module, "register") and callable(module.register) and register:
            try:
                from core.tool_registry import tool_registry
                module.register(tool_registry)
            except Exception as exc:
                log.warning(f"[plugins] {path.name}.register() failed: {exc}")
        discovered.extend(entries)

    with _lock:
        for entry in discovered:
            name = entry["name"]
            if name in _loaded_names:
                continue
            if register:
                try:
                    from core.tool_registry import tool_registry
                    tool_registry.register(
                        name,
                        entry["handler"],
                        risk=entry["risk"],
                        description=entry["description"],
                        category=entry["category"],
                    )
                    log.info(f"[plugins] registered tool {name!r} from {entry['source']}")
                except Exception as exc:
                    log.warning(f"[plugins] register {name!r} failed: {exc}")
                    continue
            _loaded.append(entry)
            _loaded_names.add(name)
        snapshot = list(_loaded)
    return snapshot


def get_plugin_declarations() -> List[dict]:
    """Return Gemini Live-compatible tool declaration dicts for plugins."""
    decls = []
    for entry in load_plugins(register=False):
        decls.append({
            "name": entry["name"],
            "behavior": entry.get("behavior", "NON_BLOCKING"),
            "description": entry["description"],
            "parameters": entry.get("parameters") or {
                "type": "OBJECT",
                "properties": {},
                "required": [],
            },
        })
    return decls


def list_plugins() -> List[str]:
    return [e["name"] for e in load_plugins(register=False)]


__all__ = [
    "load_plugins",
    "get_plugin_declarations",
    "list_plugins",
]
