"""
automation/registry.py — centralized Capability Registry.

Providers call `registry.register(...)` at import time (cheap — just
appends a dataclass to a dict, no I/O, no device probing). Nothing
expensive happens until a capability is actually invoked, so importing
every provider module costs ~microseconds and keeps idle CPU/RAM at
zero, satisfying the "lazy loading" requirement.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Optional

from utils.logger import get_logger
from automation.models import Capability

log = get_logger(__name__)


class CapabilityRegistry:
    """Process-wide singleton. Thread-safe registration + lookup."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._capabilities: Dict[str, Capability] = {}
        self._modules: Dict[str, List[str]] = {}  # module -> [capability names]

    def register(self, capability: Capability) -> None:
        with self._lock:
            if capability.name in self._capabilities:
                log.debug(f"Capability '{capability.name}' re-registered (overwrite)")
            self._capabilities[capability.name] = capability
            module = capability.name.split(".", 1)[0]
            self._modules.setdefault(module, [])
            if capability.name not in self._modules[module]:
                self._modules[module].append(capability.name)

    def register_many(self, capabilities: Iterable[Capability]) -> None:
        for c in capabilities:
            self.register(c)

    def get(self, name: str) -> Optional[Capability]:
        with self._lock:
            return self._capabilities.get(name)

    def all(self) -> List[Capability]:
        with self._lock:
            return list(self._capabilities.values())

    def modules(self) -> List[str]:
        with self._lock:
            return list(self._modules.keys())

    def find_by_keywords(self, text: str) -> List[Capability]:
        """Naive but fast keyword scorer used by the goal parser fallback.
        Real matching happens in engine.py's rule table first; this is
        the generic net for anything not explicitly patterned."""
        text_l = text.lower()
        scored: List[tuple] = []
        with self._lock:
            for cap in self._capabilities.values():
                score = 0
                for kw in cap.keywords:
                    if kw in text_l:
                        score += 1
                if score:
                    scored.append((score, cap))
        scored.sort(key=lambda t: -t[0])
        return [c for _, c in scored]


# Process-wide singleton — every provider imports this same instance.
registry = CapabilityRegistry()
