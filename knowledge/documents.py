"""
knowledge/documents.py — Lightweight content extraction for indexing
=======================================================================
Pulls a small text excerpt out of a file so the Knowledge Index has
something to embed and preview. This is deliberately NOT the same job
as `actions/file_processor.py` (which calls Gemini to *understand* a
file's content on demand). Extraction here:

  * Never calls an LLM or the network — indexing thousands of files
    can't cost thousands of API calls or the "lightweight" requirement
    is dead on arrival.
  * Is capped hard on bytes read and chars returned — a 2GB video file
    or a 500-page PDF should cost the same few milliseconds as a small
    text file, not scale with file size.
  * Fails soft — a library not being installed (python-docx, pypdf,
    etc.) or a corrupt/locked file degrades to "index filename only",
    never raises up into the indexer and stalls a whole scan.

`file_processor.py`'s summarize/describe/explain calls stay the
on-demand, LLM-powered layer for when the user actually asks about a
specific file — this module only feeds the passive background index.

Author : Gama Knowledge Layer
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

# Hard caps — keep indexing O(1)-ish per file regardless of file size.
MAX_READ_BYTES = 200_000     # don't read more than ~200KB of any file
MAX_EXCERPT_CHARS = 4000     # excerpt stored/embedded is capped further

TEXT_EXTS = {"txt", "md", "rst", "log", "csv", "tsv", "json", "yaml", "yml", "xml", "ini", "cfg"}
CODE_EXTS = {
    "py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c", "cpp", "h", "hpp",
    "cs", "go", "rs", "rb", "php", "swift", "kt", "sh", "bash", "ps1", "lua", "sql",
}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"}

# Modules that fail to import are remembered so we don't retry (and
# re-log) the same failed import on every single file of that type.
_missing_libs: set[str] = set()


def _try_import(name: str):
    if name in _missing_libs:
        return None
    try:
        return __import__(name)
    except Exception:
        _missing_libs.add(name)
        log.info(f"Optional dependency '{name}' not installed — that file type will "
                  f"be indexed by filename/metadata only.")
        return None


def extract_excerpt(path: Path, ext: str) -> str:
    """Return a short text excerpt for embedding/preview, or '' if the
    file type isn't handled / extraction fails. Never raises."""
    try:
        if ext in TEXT_EXTS or ext in CODE_EXTS:
            return _extract_plain(path)
        if ext == "pdf":
            return _extract_pdf(path)
        if ext in ("docx", "doc"):
            return _extract_docx(path)
        if ext in ("pptx", "ppt"):
            return _extract_pptx(path)
        if ext in ("xlsx", "xls"):
            return _extract_xlsx(path)
        # images/audio/video/binaries: no cheap local text extraction —
        # indexed by filename + metadata + (optionally) vision model later
        return ""
    except Exception as exc:
        log.debug(f"Extraction failed for {path}: {exc}")
        return ""


def _extract_plain(path: Path) -> str:
    with open(path, "rb") as f:
        raw = f.read(MAX_READ_BYTES)
    return raw.decode("utf-8", errors="ignore")[:MAX_EXCERPT_CHARS]


def _extract_pdf(path: Path) -> str:
    # Prefer pymupdf (fitz) when available — faster and better layout.
    fitz = _try_import("fitz")
    if fitz is not None:
        try:
            doc = fitz.open(str(path))
            chunks = []
            total = 0
            for i, page in enumerate(doc):
                if i >= 15:
                    break
                text = page.get_text("text") or ""
                chunks.append(text)
                total += len(text)
                if total >= MAX_EXCERPT_CHARS:
                    break
            doc.close()
            return "\n".join(chunks)[:MAX_EXCERPT_CHARS]
        except Exception as exc:
            log.debug(f"pymupdf extraction failed for {path}: {exc}")

    pypdf = _try_import("pypdf") or _try_import("PyPDF2")
    if pypdf is None:
        return ""
    try:
        reader = pypdf.PdfReader(str(path))
        chunks = []
        total = 0
        for page in reader.pages[:15]:  # cap pages scanned — first 15 is enough to embed/preview
            text = page.extract_text() or ""
            chunks.append(text)
            total += len(text)
            if total >= MAX_EXCERPT_CHARS:
                break
        return "\n".join(chunks)[:MAX_EXCERPT_CHARS]
    except Exception as exc:
        log.debug(f"PDF extraction failed for {path}: {exc}")
        return ""


def _extract_docx(path: Path) -> str:
    docx = _try_import("docx")  # python-docx
    if docx is None:
        return ""
    try:
        doc = docx.Document(str(path))
        parts = []
        total = 0
        for para in doc.paragraphs:
            parts.append(para.text)
            total += len(para.text)
            if total >= MAX_EXCERPT_CHARS:
                break
        return "\n".join(parts)[:MAX_EXCERPT_CHARS]
    except Exception as exc:
        log.debug(f"DOCX extraction failed for {path}: {exc}")
        return ""


def _extract_pptx(path: Path) -> str:
    pptx = _try_import("pptx")  # python-pptx
    if pptx is None:
        return ""
    try:
        prs = pptx.Presentation(str(path))
        parts = []
        total = 0
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    parts.append(shape.text)
                    total += len(shape.text)
            if total >= MAX_EXCERPT_CHARS:
                break
        return "\n".join(parts)[:MAX_EXCERPT_CHARS]
    except Exception as exc:
        log.debug(f"PPTX extraction failed for {path}: {exc}")
        return ""


def _extract_xlsx(path: Path) -> str:
    openpyxl = _try_import("openpyxl")
    if openpyxl is None:
        return ""
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        total = 0
        for ws in wb.worksheets[:3]:  # cap sheets scanned
            parts.append(f"[sheet: {ws.title}]")
            for row in ws.iter_rows(max_row=50, values_only=True):  # cap rows scanned
                line = " ".join(str(c) for c in row if c is not None)
                if line:
                    parts.append(line)
                    total += len(line)
                if total >= MAX_EXCERPT_CHARS:
                    break
            if total >= MAX_EXCERPT_CHARS:
                break
        return "\n".join(parts)[:MAX_EXCERPT_CHARS]
    except Exception as exc:
        log.debug(f"XLSX extraction failed for {path}: {exc}")
        return ""


def guess_category(ext: str, path: Path) -> Optional[str]:
    """Cheap heuristic categorization by extension + path hints — the
    'smart categorization' the spec asks for, without an LLM call per
    file. Good enough for filtering; File Intelligence can refine a
    single file's category on demand with a real model call later."""
    ext = ext.lower()
    parts_lower = {p.lower() for p in path.parts}

    if ext in IMAGE_EXTS:
        return "Images"
    if ext in CODE_EXTS:
        return "Code"
    if ext in ("zip", "rar", "7z", "tar", "gz"):
        return "Archive"
    if ext in ("exe", "msi", "dmg", "pkg", "appimage"):
        return "Installers"
    if ext in ("pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls") or ext in TEXT_EXTS:
        if any(h in parts_lower for h in ("invoice", "invoices", "billing", "receipts")):
            return "Invoices"
        if any(h in parts_lower for h in ("study", "notes", "college", "school", "university", "class")):
            return "Study"
        if any(h in parts_lower for h in ("project", "projects", "work")):
            return "Projects"
        return "Documents"
    return None


__all__ = ["extract_excerpt", "guess_category", "TEXT_EXTS", "CODE_EXTS", "IMAGE_EXTS"]
