"""
core/protocols/registry.py — Identifier resolution & lookup for Protocols
================================================================================
Lets a Protocol be found by numeric id ("Protocol 17"), spoken number
("Protocol seventeen"), or free-form name ("Coding Protocol" / "coding"),
and supports fuzzy search across names/descriptions/tags.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, Union

from core.protocols.models import Protocol
from core.protocols.storage import protocol_storage

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


def normalize_identifier(identifier: str) -> Tuple[Optional[int], str]:
    """Normalize a raw identifier string into (numeric_id, name_slug).

    Accepts: "17", "Protocol 17", "protocol number 17", "Coding Protocol",
    "coding", "seventeen". Returns (None, slug) when no number is present.
    """
    if identifier is None:
        return None, ""
    text = str(identifier).strip().lower()
    text = re.sub(r"^protocol\s+(number\s+)?", "", text)
    text = re.sub(r"\s+protocol$", "", text).strip()

    if text.isdigit():
        return int(text), ""

    m = re.fullmatch(r"#?(\d+)", text)
    if m:
        return int(m.group(1)), ""

    if text in _NUM_WORDS:
        return _NUM_WORDS[text], ""

    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return None, slug


class ProtocolRegistry:
    """Thin resolution layer over ProtocolStorage. No caching beyond what
    storage already holds in memory — protocol counts are small, so a
    linear scan is fine and keeps this correct-by-construction."""

    def resolve(self, identifier: Union[str, int]) -> Optional[Protocol]:
        if identifier is None or identifier == "":
            return None
        if isinstance(identifier, int):
            num_id, slug = identifier, ""
        else:
            num_id, slug = normalize_identifier(identifier)

        protocols = protocol_storage.get_all()

        if num_id is not None:
            for p in protocols:
                if p.numeric_id == num_id:
                    return p

        if slug:
            for p in protocols:
                if re.sub(r"[^a-z0-9]+", "_", p.display_name.lower()).strip("_") == slug:
                    return p
            # Loose contains-match fallback so "coding" matches "Coding Protocol".
            for p in protocols:
                norm_name = re.sub(r"[^a-z0-9]+", "_", p.display_name.lower()).strip("_")
                if slug in norm_name or norm_name in slug:
                    return p

        # Last resort: exact id match (internal uuid), useful for programmatic callers.
        for p in protocols:
            if p.id == str(identifier):
                return p

        return None

    def search(self, query: str) -> List[Protocol]:
        if not query:
            return []
        q = query.strip().lower()
        protocols = protocol_storage.get_all()

        def score(p: Protocol) -> int:
            hay = " ".join([p.display_name.lower(), p.description.lower(), p.category.lower(), " ".join(p.tags)])
            if q == p.display_name.lower():
                return 100
            if q in p.display_name.lower():
                return 80
            if q in hay:
                return 50
            # crude fuzzy: fraction of query characters present in order
            it = iter(hay)
            if all(ch in it for ch in q):
                return 20
            return 0

        scored = [(score(p), p) for p in protocols]
        scored = [(s, p) for s, p in scored if s > 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored]


protocol_registry = ProtocolRegistry()

__all__ = ["ProtocolRegistry", "protocol_registry", "normalize_identifier"]
