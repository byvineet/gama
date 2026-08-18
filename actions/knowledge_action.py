"""
actions/knowledge_action.py — Knowledge Layer tool surface
==============================================================
Thin dispatcher exposing knowledge/ to Gama's function-calling loop,
following the same `action(action_name, **kwargs) -> str` shape as
actions/file_controller.py and core.task_queue so it
plugs into main.py's existing tool dispatch without a new pattern.

Every function here returns a short, structured string (not raw
FileRecord/SearchResult objects) — per the spec's "keep responses
structured, avoid raw filesystem objects" and because these strings
go straight into an LLM context window, so verbosity has a real cost.

Author : Gama Knowledge Layer
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from knowledge import index as kindex
from knowledge import ranking, watcher
from utils.logger import get_logger

log = get_logger(__name__)

MAX_RESULTS_SHOWN = 8


def knowledge_action(action: str, **kwargs) -> str:
    action = (action or "").lower().strip()
    if action == "search":
        return _search(kwargs.get("query", ""), kwargs.get("ext"),
                       kwargs.get("project"), kwargs.get("category"))
    if action == "find_related":
        return _find_related(kwargs.get("path", ""))
    if action == "open":
        return _open(kwargs.get("path", ""))
    if action == "index_now" or action == "reindex":
        return _index_now(kwargs.get("folders") or kwargs.get("path"),
                          force=bool(kwargs.get("force")) or action == "reindex")
    if action == "stats":
        return _stats()
    return (f"Unknown knowledge action: {action}. Use: search, find_related, "
            f"open, index_now (alias: reindex), stats.")


def _format_results(results, header: str) -> str:
    if not results:
        return f"{header}\nNo matching files found. (If this folder hasn't been indexed yet, try 'index_now'.)"
    lines = [header]
    for r in results[:MAX_RESULTS_SHOWN]:
        rec = r.record
        preview = (rec.summary or rec.text_excerpt or "").strip().replace("\n", " ")[:100]
        preview = f" — {preview}..." if preview else ""
        lines.append(f"  • {rec.path}{preview}")
    if len(results) > MAX_RESULTS_SHOWN:
        lines.append(f"  ... and {len(results) - MAX_RESULTS_SHOWN} more")
    return "\n".join(lines)


def _search(query: str, ext: Optional[str], project: Optional[str],
           category: Optional[str]) -> str:
    if not query.strip():
        return "Search failed: no query given."
    candidates = kindex.semantic_search(query, ext=ext, project=project, category=category)
    ranked = ranking.rerank(candidates)
    return _format_results(ranked, f"Search results for '{query}':")


def _find_related(path: str) -> str:
    """Files semantically similar to a given file — 'find files related
    to this one', duplicate/near-duplicate detection, project grouping."""
    if not path:
        return "find_related failed: no path given."
    rec = kindex.get_by_path(path)
    if rec is None:
        return f"'{path}' isn't indexed yet — try 'index_now' on its folder first."
    seed_query = f"{rec.filename} {rec.text_excerpt or ''}"[:1000]
    candidates = kindex.semantic_search(seed_query, limit=kindex.DEFAULT_SEARCH_LIMIT + 1)
    candidates = [r for r in candidates if r.record.path != path]  # exclude itself
    ranked = ranking.rerank(candidates)
    return _format_results(ranked, f"Files related to {rec.filename}:")


def _open(path: str) -> str:
    """Instant open of an indexed file or smart resolution of natural language file references
    (e.g., 'trigonometric functions latest pdf', 'budget report'). Bumps usage stats so ranking improves.
    """
    target_path = None
    if not path:
        return "open failed: no file path or query given."

    # 0. Check for "latest" / "newest" / "recent" requests (ordered strictly by date & time)
    path_lower = path.lower()
    if any(w in path_lower for w in ("latest", "newest", "most recent", "recent")):
        clean_pat = path_lower
        for w in ("open", "latest", "newest", "most recent", "recent", "the", "file", "pdf", "doc", "document"):
            clean_pat = clean_pat.replace(w, " ")
        clean_pat = clean_pat.strip()
        latest_rec = kindex.get_latest_file(pattern=clean_pat if clean_pat else None)
        if latest_rec:
            target_path = latest_rec.path

    # 1. Direct index path lookup
    if not target_path:
        rec = kindex.get_by_path(path)
        if rec:
            target_path = rec.path

    # 2. Direct filesystem path resolution
    if not target_path:
        from utils.windows_paths import resolve_user_path
        p = resolve_user_path(path)
        if p.exists() and p.is_file():
            target_path = str(p)

    # 3. Context resolver (recent downloads, recent files, semantic references)
    if not target_path:
        try:
            from actions.context_resolver import resolve_file_reference
            res = resolve_file_reference(path)
            if res.status == "resolved" and res.path and res.path.exists() and res.path.is_file():
                target_path = str(res.path)
        except Exception:
            pass


    # 4. Filename substring search in index
    if not target_path:
        try:
            filename_hits = kindex.filename_search(path, limit=3)
            if filename_hits:
                target_path = filename_hits[0].path
        except Exception:
            pass

    # 5. Semantic search in index
    if not target_path:
        try:
            sem_hits = kindex.semantic_search(path, limit=3)
            if sem_hits:
                target_path = sem_hits[0].record.path
        except Exception:
            pass

    if not target_path:
        return f"Could not find any file matching '{path}'. Try indexing its folder first with 'index_now'."

    kindex.touch_access(target_path)
    import os
    import subprocess
    try:
        if os.name == "nt":
            os.startfile(target_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", target_path])
        return f"Opened: {target_path}"
    except Exception as exc:
        return f"Open failed for '{target_path}': {exc}"


def _index_now(folders, force: bool = False) -> str:
    """User explicitly asked to (re)index something now — ANY folder,
    not just Desktop/Documents/Downloads: a keyword ("desktop"), a
    relative name, or a full path ("D:/Projects/Thesis") all work,
    since watcher.scan_once resolves via utils.windows_paths.resolve_user_path.
    Runs as a proper background task (watcher.schedule_background_scan),
    so this call returns immediately rather than blocking the voice
    loop on a full folder scan.

    `force=True` (or action='reindex') re-embeds every file in scope
    even if it hasn't changed since last time — use this when the user
    explicitly says "re-index" rather than just "check for new files".
    """
    if isinstance(folders, str):
        folders = [folders]

    if folders:
        from pathlib import Path as _Path
        from utils.windows_paths import resolve_user_path
        missing = [f for f in folders if not resolve_user_path(f).exists()]
        if missing:
            return (f"Can't find: {', '.join(missing)}. Give a valid folder "
                    f"name or full path and I'll index it.")

    task_id = watcher.schedule_background_scan(folders, force=force)
    scope = ", ".join(folders) if folders else "your default folders (Desktop/Documents/Downloads)"
    verb = "Re-indexing" if force else "Indexing"
    return f"{verb} {scope} in the background ({task_id}). I'll keep working while it runs."


def _stats() -> str:
    s = kindex.stats()
    return (f"Knowledge index: {s['indexed_files']} files indexed "
            f"({s['embedded_files']} searchable), {s['tombstoned']} tombstoned entries pending cleanup.")


__all__ = ["knowledge_action"]
