"""
core/ui_controller.py — Thin UI state helpers
=============================================
Keeps log/state writes consistent so session/audio paths do not each
invent their own HTML fragments.
"""

from __future__ import annotations

from typing import Any, Optional

from utils.logger import get_logger

log = get_logger(__name__)


class UIController:
    def __init__(self, ui: Any = None) -> None:
        self._ui = ui

    def attach(self, ui: Any) -> None:
        self._ui = ui

    def set_state(self, state: str) -> None:
        if self._ui is None:
            return
        try:
            self._ui.set_state(state)
        except Exception as exc:
            log.debug(f"UI set_state failed: {exc}")

    def log(self, html: str) -> None:
        if self._ui is None:
            return
        try:
            self._ui.write_log(html)
        except Exception:
            pass

    def observe_banner(self, wake_phrase: str = "gama") -> None:
        self.set_state("IDLE")
        self.log(
            f'<span style="color:#5ab8cc">👁 Observing — listening only. '
            f'Say "{wake_phrase}" or address me by name.</span>'
        )

    def awake_banner(self) -> None:
        self.log('<span style="color:#00ff88">⚡ Gama is awake!</span>')

    def answering_pending_banner(self) -> None:
        self.log(
            '<span style="color:#00ff88">⚡ Answering what you asked while observing…</span>'
        )

    def interrupted_banner(self) -> None:
        self.log('<span style="color:#007AFF">[interrupted]</span>')


__all__ = ["UIController"]
