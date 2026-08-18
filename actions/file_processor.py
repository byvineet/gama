"""
actions/file_processor.py — Gama Universal File Processor
Process images, PDFs, code, docs via Gemini and/or local extractors.

PDF:
  - extract_text / full_text / read → local text first (pymupdf/pypdf),
    then automatic Gemini multimodal fallback if local fails or libs missing
  - summarize / analyze / describe / qa → Gemini on full PDF bytes
Never opens the file in an external viewer.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import mimetypes
import sys
from pathlib import Path
from typing import Optional

log = get_logger(__name__)
logger = log  # back-compat alias
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"}
CODE_EXTS = {"py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c", "cpp",
             "cs", "go", "rs", "rb", "php", "swift", "kt", "sh", "bash", "ps1",
             "lua", "r", "m", "sql", "yaml", "toml", "json", "xml"}
TEXT_EXTS = {"txt", "md", "rst", "log", "csv", "tsv"}

PDF_MAX_PAGES = 200
PDF_MAX_CHARS = 120_000




BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in IMAGE_EXTS:
        return "image"
    if ext in CODE_EXTS:
        return "code"
    if ext in TEXT_EXTS:
        return "text"
    if ext == "pdf":
        return "pdf"
    if ext in ("docx", "doc"):
        return "docx"
    if ext in ("xlsx", "xls"):
        return "excel"
    if ext in ("pptx", "ppt"):
        return "slides"
    if ext in ("mp3", "wav", "ogg", "m4a"):
        return "audio"
    if ext in ("mp4", "avi", "mov", "mkv"):
        return "video"
    return "unknown"


def _extract_pdf_local(path: Path, max_chars: int = PDF_MAX_CHARS) -> str:
    """Local full-text extraction. Prefer pymupdf, fall back to pypdf/PyPDF2."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(str(path))
        chunks: list[str] = []
        total = 0
        for i, page in enumerate(doc):
            if i >= PDF_MAX_PAGES:
                break
            t = page.get_text("text") or ""
            if t.strip():
                chunks.append(t)
                total += len(t)
            if total >= max_chars:
                break
        doc.close()
        text = "\n".join(chunks).strip()
        if text:
            return text[:max_chars]
    except ImportError:
        logger.info("pymupdf not installed — trying pypdf")
    except Exception as exc:
        logger.debug("pymupdf extract failed: %s", exc)

    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(str(path))
        chunks = []
        total = 0
        for i, page in enumerate(reader.pages):
            if i >= PDF_MAX_PAGES:
                break
            t = page.extract_text() or ""
            if t.strip():
                chunks.append(t)
                total += len(t)
            if total >= max_chars:
                break
        text = "\n".join(chunks).strip()
        return text[:max_chars] if text else ""
    except ImportError:
        logger.info("pypdf/PyPDF2 not installed — local PDF text unavailable")
        return ""
    except Exception as exc:
        logger.debug("pypdf extract failed: %s", exc)
        return ""


def _gemini_pdf(path: Path, prompt: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=_get_api_key())
    with open(path, "rb") as f:
        pdf_bytes = f.read()
    if len(pdf_bytes) > 40 * 1024 * 1024:
        logger.warning("Large PDF (%d bytes): %s", len(pdf_bytes), path)
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt],
    )
    return response.text or "No response."


def file_processor(
    path: str,
    action: str = "auto",
    instruction: str = "",
    show_on_nexus: bool = False,
    max_chars: int = PDF_MAX_CHARS,
    **kwargs,
) -> str:
    """Process a file. Never opens an external window."""
    p = Path(path).expanduser()
    if not p.exists():
        return f"File not found: {path}"

    if kwargs.get("on_nexus") or kwargs.get("display_on_nexus") or kwargs.get("nexus"):
        show_on_nexus = True

    ftype = _detect_type(p)
    action = (action or "auto").lower().strip().replace("-", "_")
    if action == "auto":
        action = {
            "image": "describe",
            "pdf": "summarize",
            "code": "explain",
            "text": "summarize",
            "docx": "summarize",
            "excel": "analyze",
            "slides": "summarize",
            "audio": "transcribe",
            "video": "info",
        }.get(ftype, "describe")

    logger.info("Processing %s file %s with action '%s'", ftype, p.name, action)

    if ftype == "pdf":
        local_actions = {
            "extract_text", "full_text", "read", "text", "local_text", "raw_text",
        }
        want_local = action in local_actions
        try:
            max_chars_i = int(max_chars or PDF_MAX_CHARS)
        except Exception:
            max_chars_i = PDF_MAX_CHARS

        if want_local:
            text = _extract_pdf_local(p, max_chars=max_chars_i)
            if text:
                header = f"PDF: {p.name}\nExtracted {len(text)} characters (local).\n---\n"
                return header + text
            logger.info("Local PDF extract empty/unavailable — falling back to Gemini for %s", p.name)
            prompt = instruction or (
                "Extract all readable text from this PDF in reading order. "
                "Preserve headings and structure where possible. Do not summarize — "
                "return the content itself."
            )
            try:
                out = _gemini_pdf(p, prompt)
                return (
                    f"PDF: {p.name}\n"
                    f"(Extracted via Gemini — install pymupdf for faster local extract)\n"
                    f"---\n{out}"
                )
            except Exception as exc:
                return (
                    f"Could not extract text from PDF '{p.name}'. "
                    f"Local libraries missing/failed and Gemini fallback error: {exc}. "
                    "Install with: pip install pymupdf pypdf"
                )

        prompts = {
            "summarize": "Summarize this PDF in clear key points. Cover the main sections.",
            "extract_text": "Extract all main text from this PDF in reading order.",
            "describe": "Describe what this PDF is about and its structure.",
            "analyze": "Analyze this PDF: purpose, key findings, structure, and notable details.",
            "qa": instruction or "Answer questions about this PDF based on its content.",
            "answer": instruction or "Answer based on this PDF's content.",
        }
        if action in ("qa", "answer") and instruction:
            prompt = instruction
        else:
            prompt = instruction or prompts.get(action, prompts["summarize"])
        try:
            return _gemini_pdf(p, prompt)
        except Exception as exc:
            text = _extract_pdf_local(p)
            if text:
                return f"PDF: {p.name} (local fallback after Gemini error: {exc})\n---\n{text[:max_chars_i]}"
            return f"PDF processing failed: {exc}"

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=_get_api_key())

        if ftype == "image":
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            with open(p, "rb") as f:
                image_bytes = f.read()
            prompts = {
                "describe": "Describe this image in detail. What's in it? Any text?",
                "ocr": "Extract ALL text visible in this image, line by line.",
                "summarize": "Summarize what this image shows in 2-3 sentences.",
                "analyze": "Analyze this image: objects, people, text, colors, mood.",
            }
            prompt = instruction or prompts.get(action, prompts["describe"])
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime), prompt],
            )
            return response.text or "No response."

        if ftype in ("code", "text", "docx", "excel", "slides"):
            text = p.read_text(encoding="utf-8", errors="ignore")[:30000]
            prompts = {
                "summarize": f"Summarize this {ftype} file:\n\n{text}",
                "explain": f"Explain this {ftype} file:\n\n{text}",
                "analyze": f"Analyze this {ftype} file:\n\n{text}",
                "review": f"Review this for bugs/improvements:\n\n{text}",
            }
            prompt = instruction or prompts.get(action, prompts["summarize"])
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite", contents=prompt,
            )
            return response.text or "No response."

        return f"Unsupported file type: {ftype}"
    except Exception as exc:
        return f"File processing failed: {exc}"


__all__ = ["file_processor"]
