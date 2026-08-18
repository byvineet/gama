"""
actions/web_reader.py — Read weblinks and extract clean page content
=====================================================================
Fetches a URL and returns main readable content (title, text, meta)
without opening a browser window for the user.

Strategy:
  1. Fast path: httpx/requests + trafilatura (or readability / BS4 fallback)
  2. Optional Playwright fallback for JS-heavy pages when extraction is weak

Never auto-opens the page on the user's desktop. Results can optionally
be pushed to Gama Nexus via display_stage.

Author : Gama Nexus / Web Reader
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

log = logging.getLogger("gama.web_reader")

DEFAULT_MAX_CHARS = 12_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 GamaReader/1.0"
)


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _http_get(url: str, timeout: float = 20.0) -> tuple[str, str, int]:
    """Return (final_url, html_text, status_code)."""
    try:
        import httpx
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            r = client.get(url)
            return str(r.url), r.text, r.status_code
    except ImportError:
        pass
    except Exception as exc:
        log.debug("httpx get failed: %s", exc)

    try:
        import requests
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True,
        )
        return str(r.url), r.text, r.status_code
    except Exception as exc:
        raise RuntimeError(f"HTTP fetch failed: {exc}") from exc


def _extract_trafilatura(html: str, url: str) -> Dict[str, Any]:
    try:
        import trafilatura
        downloaded = html
        meta = trafilatura.extract_metadata(downloaded, default_url=url)
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            url=url,
        ) or ""
        title = ""
        if meta is not None:
            title = getattr(meta, "title", None) or ""
        if not title:
            # light fallback title
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
        return {
            "title": title or "",
            "text": text.strip(),
            "extractor": "trafilatura",
        }
    except ImportError:
        return {}
    except Exception as exc:
        log.debug("trafilatura failed: %s", exc)
        return {}


def _extract_bs4(html: str) -> Dict[str, Any]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}
    try:
        soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        # Prefer article / main
        main = soup.find("article") or soup.find("main") or soup.body
        text = main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True)
        # Collapse blank lines
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        text = "\n".join(lines)
        return {"title": title, "text": text, "extractor": "bs4"}
    except Exception as exc:
        log.debug("bs4 extract failed: %s", exc)
        return {}


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def _extract_readability(html: str, url: str) -> Dict[str, Any]:
    try:
        from readability import Document
    except ImportError:
        return {}
    try:
        doc = Document(html)
        title = (doc.short_title() or doc.title() or "").strip()
        summary_html = doc.summary() or ""
        try:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(summary_html, "html.parser").get_text("\n", strip=True)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", summary_html)
            text = re.sub(r"\s+", " ", text).strip()
        return {"title": title, "text": text, "extractor": "readability"}
    except Exception as exc:
        log.debug("readability failed: %s", exc)
        return {}


def _playwright_fetch(url: str, timeout_ms: int = 25000) -> tuple[str, str]:
    """Return (final_url, html) via headless Playwright. Optional heavy path."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright not installed") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Small settle for late content
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            html = page.content()
            final = page.url
            return final, html
        finally:
            browser.close()


def fetch_webpage(
    url: str,
    mode: str = "main_content",
    max_chars: int = DEFAULT_MAX_CHARS,
    use_playwright: bool = False,
    show_on_nexus: bool = False,
    title: str = "",
) -> str:
    """
    Read a weblink and return clean text content.

    mode:
      - main_content (default): main article / readable text
      - full: larger dump (still capped)
      - title_only: just title + url

    show_on_nexus: if True, also push a summary card to Gama Nexus.
    """
    url = _normalize_url(url)
    if not url or not _is_valid_url(url):
        return "Please provide a valid http(s) URL."

    mode = (mode or "main_content").lower().strip()
    try:
        max_chars = int(max_chars or DEFAULT_MAX_CHARS)
    except Exception:
        max_chars = DEFAULT_MAX_CHARS
    max_chars = max(500, min(max_chars, 50_000))

    final_url = url
    html = ""
    status = 0
    extractor = "none"

    try:
        if use_playwright:
            final_url, html = _playwright_fetch(url)
            status = 200
        else:
            final_url, html, status = _http_get(url)
            if status >= 400:
                return f"Could not fetch page (HTTP {status}): {final_url}"
    except Exception as exc:
        # One retry with Playwright if available and not already used
        if not use_playwright:
            try:
                log.info("HTTP failed (%s); trying Playwright for %s", exc, url)
                final_url, html = _playwright_fetch(url)
                status = 200
            except Exception as exc2:
                return f"Failed to fetch webpage: {exc}. Playwright fallback also failed: {exc2}"
        else:
            return f"Failed to fetch webpage: {exc}"

    if not html or len(html) < 40:
        return f"Page returned little or no HTML content: {final_url}"

    extracted: Dict[str, Any] = {}
    for fn in (_extract_trafilatura, _extract_readability, _extract_bs4):
        extracted = fn(html, final_url) if fn is not _extract_bs4 else fn(html)
        if extracted.get("text") and len(extracted["text"]) > 80:
            break
        if extracted.get("text") and mode == "title_only":
            break

    # Weak extraction → try Playwright once if we didn't already
    if (not extracted.get("text") or len(extracted.get("text", "")) < 80) and not use_playwright:
        try:
            log.info("Weak extraction; retrying with Playwright for %s", url)
            final_url, html = _playwright_fetch(url)
            for fn in (_extract_trafilatura, _extract_readability, _extract_bs4):
                extracted = fn(html, final_url) if fn is not _extract_bs4 else fn(html)
                if extracted.get("text") and len(extracted["text"]) > 80:
                    break
        except Exception as exc:
            log.debug("Playwright retry failed: %s", exc)

    page_title = (title or extracted.get("title") or "").strip() or final_url
    text = (extracted.get("text") or "").strip()
    extractor = extracted.get("extractor") or "none"

    if mode == "title_only":
        result = f"Title: {page_title}\nURL: {final_url}"
        if show_on_nexus:
            _push_nexus_card(page_title, f"URL: {final_url}", final_url)
        return result

    if not text:
        return (
            f"Could not extract readable content from {final_url}. "
            "The page may be heavily JavaScript-rendered or blocked."
        )

    if mode != "full" and len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n… [truncated]"
    elif len(text) > max_chars:
        text = text[:max_chars] + "\n… [truncated]"

    result = (
        f"Title: {page_title}\n"
        f"URL: {final_url}\n"
        f"Extractor: {extractor}\n"
        f"Characters: {len(text)}\n"
        f"---\n"
        f"{text}"
    )

    if show_on_nexus:
        preview = text[:900] + ("…" if len(text) > 900 else "")
        _push_nexus_card(page_title, preview, final_url)

    return result


def _push_nexus_card(title: str, content: str, url: str = "") -> None:
    try:
        from actions.display_stage import display_stage
        meta = [{"label": "Source", "value": url}] if url else []
        display_stage(
            action="information",
            title=title or "Web page",
            content=content,
            metadata=meta,
            scene_id="web-reader-card",
        )
    except Exception as exc:
        log.debug("Nexus push failed: %s", exc)


def web_reader(
    url: str = "",
    action: str = "read",
    mode: str = "main_content",
    max_chars: int = DEFAULT_MAX_CHARS,
    use_playwright: bool = False,
    show_on_nexus: bool = False,
    title: str = "",
    **kwargs,
) -> str:
    """
    Public tool entry.

    action:
      - read (default): fetch and extract
      - title: title only
    """
    action = (action or "read").lower().strip()
    url = url or kwargs.get("link") or kwargs.get("page") or ""
    if action in ("title", "title_only"):
        mode = "title_only"
    if kwargs.get("on_nexus") or kwargs.get("display_on_nexus") or kwargs.get("nexus"):
        show_on_nexus = True
    return fetch_webpage(
        url=url,
        mode=mode,
        max_chars=max_chars,
        use_playwright=bool(use_playwright or kwargs.get("playwright")),
        show_on_nexus=bool(show_on_nexus),
        title=title,
    )


__all__ = ["web_reader", "fetch_webpage"]
