"""
utils/lazy.py — Shared lazy-import helper
"""
from __future__ import annotations
import importlib
from typing import Any, Callable

def lazy_import(module_path: str, attr: str) -> Callable[..., Any]:
    box: dict = {}
    def _resolve():
        obj = box.get("obj")
        if obj is None:
            obj = getattr(importlib.import_module(module_path), attr)
            box["obj"] = obj
        return obj
    def _wrapper(*args, **kwargs):
        return _resolve()(*args, **kwargs)
    _wrapper.__name__ = attr  # type: ignore[attr-defined]
    return _wrapper

_lazy_import = lazy_import
__all__ = ["lazy_import", "_lazy_import"]
