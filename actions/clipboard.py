"""
actions/clipboard.py — Gama Clipboard Manager (merged)
=======================================================
Read, write, clear the system clipboard, smart history, content
intelligence, and AI pipeline (summarize/translate/fix/rewrite).
Gama Clipboard Manager.

"""

from __future__ import annotations

from utils.logger import get_logger

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, List

log = get_logger(__name__)
logger = log

# ─── History  ───────────────────────────────────────
_MAX_ENTRIES = 40
_MAX_TEXT_CHARS = 8000
_POLL_INTERVAL_S = 1.25
_STORE_NAME = "clipboard_history.json"
_lock = threading.RLock()
_entries: list = []
_last_seen: str = ""
_monitor_started = False
_monitor_stop = threading.Event()

def _base_dir() -> Path:
    try:
        from utils.paths import get_base_dir
        p = Path(get_base_dir())
    except Exception:
        p = Path(__file__).resolve().parents[1] / "storage"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _store_path() -> Path:
    return _base_dir() / _STORE_NAME

@dataclass
class ClipEntry:
    id: int
    text: str
    kind: str = "text"
    preview: str = ""
    ts: float = field(default_factory=time.time)
    def to_dict(self) -> dict:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: dict) -> "ClipEntry":
        return cls(id=int(d.get("id", 0)), text=str(d.get("text", "")), kind=str(d.get("kind", "text")), preview=str(d.get("preview", "")), ts=float(d.get("ts", time.time())))

def _classify(text: str) -> str:
    t = (text or "").strip()
    if not t: return "empty"
    if re.match(r"^https?://\S+$", t, re.I) or re.match(r"^www\.\S+$", t, re.I): return "url"
    if re.match(r"^[A-Za-z]:\\", t) or t.startswith("\\\\") or t.startswith("/home/") or t.startswith("/Users/"): return "path"
    if "def " in t or "class " in t or "import " in t or "function " in t: return "code"
    return "text"

def _preview(text: str, n: int = 80) -> str:
    t = (text or "").replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else t

def _load() -> None:
    global _entries
    path = _store_path()
    if not path.exists():
        _entries = []
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _entries = [ClipEntry.from_dict(x) for x in data.get("entries", [])][-_MAX_ENTRIES:]
    except Exception as exc:
        log.debug("clipboard history load failed: %s", exc)
        _entries = []

def _save() -> None:
    try:
        path = _store_path()
        path.write_text(json.dumps({"entries": [e.to_dict() for e in _entries]}, indent=0), encoding="utf-8")
    except Exception as exc:
        log.debug("clipboard history save failed: %s", exc)

def _push(text: str) -> None:
    global _entries
    text = (text or "")[:_MAX_TEXT_CHARS]
    if not text.strip(): return
    with _lock:
        if _entries and _entries[-1].text == text: return
        nid = (_entries[-1].id + 1) if _entries else 1
        _entries.append(ClipEntry(id=nid, text=text, kind=_classify(text), preview=_preview(text)))
        _entries = _entries[-_MAX_ENTRIES:]
        _save()

def clear_history() -> None:
    global _entries
    with _lock:
        _entries = []
        _save()

def status() -> dict:
    with _lock:
        last = _entries[-1].preview if _entries else ""
        return {"count": len(_entries), "last_preview": last}

def list_history(limit: int = 10) -> list:
    with _lock:
        return list(reversed(_entries[-limit:]))

def get_entry(index: int = 1) -> Optional[ClipEntry]:
    with _lock:
        if not _entries: return None
        # 1 = most recent
        idx = -index
        if abs(idx) > len(_entries): return None
        return _entries[idx]

def search_history(query: str, limit: int = 5) -> list:
    q = (query or "").lower()
    with _lock:
        hits = [e for e in reversed(_entries) if q in e.text.lower()][:limit]
        return hits

def start_monitor() -> None:
    global _monitor_started
    if _monitor_started: return
    _monitor_started = True
    _load()
    def _loop():
        global _last_seen
        while not _monitor_stop.is_set():
            try:
                import pyperclip
                t = pyperclip.paste() or ""
                if t and t != _last_seen:
                    _last_seen = t
                    _push(t)
            except Exception:
                pass
            _monitor_stop.wait(_POLL_INTERVAL_S)
    threading.Thread(target=_loop, name="clip-hist", daemon=True).start()

# ─── Engine  ──────────────────────────────
@dataclass
class ClipboardIntelligence:
    content_type: str = "text"
    raw_text: str = ""
    suggested_actions: List[str] = field(default_factory=list)

def get_clipboard_intelligence() -> ClipboardIntelligence:
    try:
        import pyperclip
        text = pyperclip.paste() or ""
    except Exception:
        text = ""
    kind = _classify(text)
    suggestions = []
    if kind == "url":
        suggestions = ["open", "summarize", "download"]
    elif kind == "code":
        suggestions = ["fix_grammar", "explain", "run"]
    elif kind == "path":
        suggestions = ["open", "list"]
    elif kind == "text" and len(text) > 200:
        suggestions = ["summarize", "translate", "rewrite"]
    else:
        suggestions = ["copy", "clear"]
    return ClipboardIntelligence(content_type=kind, raw_text=text[:2000], suggested_actions=suggestions)

# ─── AI pipeline  ────────────────────────────────────────
def clipboard_ai(action: str = "summarize", language: str = "English", write_back: bool = False) -> str:
    try:
        import pyperclip
        text = pyperclip.paste() or ""
    except Exception as exc:
        return f"Could not read clipboard: {exc}"
    if not text.strip():
        return "Clipboard is empty."
    action = (action or "summarize").lower()
    prompt_map = {
        "summarize": f"Summarize the following text concisely:\n\n{text[:6000]}",
        "translate": f"Translate the following text to {language}:\n\n{text[:6000]}",
        "fix_grammar": f"Fix grammar and spelling, keep meaning:\n\n{text[:6000]}",
        "rewrite": f"Rewrite more clearly and professionally:\n\n{text[:6000]}",
    }
    prompt = prompt_map.get(action, prompt_map["summarize"])
    try:
        # Use whatever LLM path Gama has; fallback message if unavailable
        from core.llm_local import generate as _gen
        result = _gen(prompt) if callable(_gen) else None
        if not result:
            raise RuntimeError("no llm")
    except Exception:
        try:
            # Alternative path
            result = f"[AI {action} of clipboard content — integrate with live LLM for full result]\nPreview: {text[:300]}"
        except Exception as exc:
            return f"AI clipboard action failed: {exc}"
    if write_back:
        try:
            import pyperclip
            pyperclip.copy(str(result))
            _push(str(result))
            return f"Done ({action}). Result written back to clipboard.\n{result[:500]}"
        except Exception:
            pass
    return str(result)[:2000]

# ─── Core clipboard ops ─────────────────────────────────────────────────────
def _get_pyperclip():
    try:
        import pyperclip
        return pyperclip, None
    except ImportError:
        return None, "tkinter"

def _read() -> str:
    pc, _ = _get_pyperclip()
    text = ""
    if pc is not None:
        try:
            text = pc.paste() or ""
        except Exception as exc:
            return f"Clipboard read failed: {exc}"
    if not text:
        return "Clipboard is empty."
    return text if len(text) < 1500 else text[:1500] + "…"

def _write(text: str) -> str:
    text = text or ""
    pc, _ = _get_pyperclip()
    if pc is not None:
        try:
            pc.copy(text)
            _push(text)
            return f"Copied to clipboard ({len(text)} chars)."
        except Exception as exc:
            return f"Clipboard write failed: {exc}"
    return "Clipboard write unavailable (pyperclip missing)."

def _clear() -> str:
    return _write("")

def _history(kwargs) -> str:
    limit = int(kwargs.get("limit") or kwargs.get("n") or 8)
    entries = list_history(limit)
    if not entries:
        return "Clipboard history is empty."
    lines = []
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. [{e.kind}] {e.preview}")
    return "Recent clipboard:\n" + "\n".join(lines)

def _paste_history(kwargs) -> str:
    idx = int(kwargs.get("index") or kwargs.get("n") or kwargs.get("entry") or 1)
    e = get_entry(idx)
    if not e:
        return f"No clipboard history entry #{idx}."
    _write(e.text)
    return f"Pasted history #{idx} to clipboard: {e.preview}"

def _search_history(kwargs) -> str:
    q = kwargs.get("query") or kwargs.get("q") or kwargs.get("text") or ""
    if not q:
        return "Provide a search query."
    hits = search_history(q)
    if not hits:
        return f"No history matches for \"{q}\"."
    lines = [f"- {h.preview}" for h in hits]
    return f"Matches for \"{q}\":\n" + "\n".join(lines)

def clipboard(action: str = "read", **kwargs) -> str:
    """Clipboard management + history + intelligence + AI."""
    action = (action or "read").lower().strip().replace("-", "_").replace(" ", "_")
    if action in ("read", "get", "show", "current"):
        return _read()
    if action in ("write", "copy", "set"):
        return _write(kwargs.get("text", "") or kwargs.get("content", ""))
    if action in ("clear", "empty"):
        return _clear()
    if action == "analyze":
        try:
            info = get_clipboard_intelligence()
            sug = ", ".join(info.suggested_actions) if info.suggested_actions else "None"
            return f"Clipboard Content Type: {info.content_type}\nSuggested Actions: {sug}\nPreview: {info.raw_text[:200]}"
        except Exception as exc:
            return f"Analyze failed: {exc}"
    if action in ("summarize", "summary", "tldr"):
        return clipboard_ai("summarize", write_back=bool(kwargs.get("write_back")))
    if action in ("translate", "translation"):
        return clipboard_ai("translate", language=kwargs.get("language") or kwargs.get("target_language") or kwargs.get("lang") or "English", write_back=bool(kwargs.get("write_back")))
    if action in ("fix_grammar", "grammar", "proofread", "correct"):
        return clipboard_ai("fix_grammar", write_back=bool(kwargs.get("write_back", True)))
    if action in ("rewrite", "improve", "polish"):
        return clipboard_ai("rewrite", write_back=bool(kwargs.get("write_back", True)))
    if action in ("ai", "smart"):
        sub = (kwargs.get("mode") or kwargs.get("op") or "summarize")
        return clipboard_ai(sub, language=kwargs.get("language", ""), write_back=bool(kwargs.get("write_back")))
    if action in ("history", "list", "recent"):
        return _history(kwargs)
    if action in ("paste", "paste_entry", "recall", "use"):
        return _paste_history(kwargs)
    if action in ("search", "find"):
        return _search_history(kwargs)
    if action in ("clear_history", "history_clear"):
        clear_history()
        return "Clipboard history cleared."
    if action == "status":
        st = status()
        return f"Clipboard history: {st['count']} entries stored. Last: {st['last_preview'] or '(none)'}."
    return "Unknown clipboard action. Use: read, write, clear, analyze, summarize, translate, fix_grammar, rewrite, history, paste, search, clear_history, status."
