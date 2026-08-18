"""
actions/web_search.py — Gama Web Search (Mark XLVII style)
Gemini grounded search + DuckDuckGo fallback. Modes: search, news, research, price, compare.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _gemini_search(query: str) -> str:
    from google import genai
    client = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )
    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text
    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _ddg_search(query: str, max_results: int = 6) -> list:
    from duckduckgo_search import DDGS
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
                "url": r.get("href", ""),
            })
    return results


def _format_ddg(query: str, results: list) -> str:
    if not results:
        return f"No results found for: {query}"
    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _gemini_headlines(n: int = 5) -> tuple:
    from google import genai
    client = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=f"Current world news: {n} headlines. Numbered list, titles only.",
        config={"tools": [{"google_search": {}}]},
    )
    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text
    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*', '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)
    return headlines[:n], raw.strip()


def web_search(query: str, mode: str = "search", items: list = None,
               aspect: str = "specs") -> str:
    """Main entry point."""
    query = (query or "").strip()
    mode = (mode or "search").lower().strip()

    if mode == "news":
        try:
            headlines, raw = _gemini_headlines(6)
            return raw if raw else "No news available."
        except Exception as e:
            logger.warning(f"News failed: {e}")
            return _format_ddg("news today", _ddg_search("news today", 6))

    if mode == "compare" and items and len(items) >= 2:
        query = f"Compare {', '.join(items)} in terms of {aspect}. Give specific facts and data."

    if mode == "research":
        query = f"Research thoroughly with current information: {query}. Provide detailed findings and sources."

    if mode == "price":
        query = f"Find current prices for: {query}. Include specific prices and sources."

    # Default search — run Gemini grounded search + DDG concurrently; use
    # whichever finishes successfully.  Typical saving: 400–800ms vs serial.
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    _gemini_result: str | None = None
    _ddg_result: str | None = None
    try:
        with ThreadPoolExecutor(max_workers=2) as _pool:
            _f_gemini = _pool.submit(_gemini_search, query)
            _f_ddg    = _pool.submit(_ddg_search, query)
            for _fut in _as_completed([_f_gemini, _f_ddg]):
                try:
                    _val = _fut.result()
                    if _fut is _f_gemini:
                        _gemini_result = _val
                    else:
                        _ddg_result = _val
                except Exception as _e:
                    logger.debug(f"web_search parallel leg failed: {_e}")
        if _gemini_result:
            return _gemini_result
        if _ddg_result:
            return _format_ddg(query, _ddg_result if isinstance(_ddg_result, list)
                               else _ddg_search(query))
    except Exception as e:
        logger.warning(f"Parallel web search failed ({e}) — falling back serial...")
    # Serial fallback
    try:
        return _gemini_search(query)
    except Exception as e:
        logger.warning(f"Gemini failed ({e}) — trying DDG...")
        results = _ddg_search(query)
        return _format_ddg(query, results)


__all__ = ["web_search"]
