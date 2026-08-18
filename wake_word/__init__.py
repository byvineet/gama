"""
Gama - Wake Word Subsystem
==========================
Always-on, offline, local wake-phrase + interrupt-word detection.

Public API:
    from wake_word import WakeWordListener, load_wake_word_config

Usage inside main.py (see GamaAssistant):
    self._wake_cfg = load_wake_word_config()
    self._wake_listener = WakeWordListener(self._wake_cfg)
    ...
    label = self._wake_listener.feed(pcm_bytes)   # inside the mic callback
    if label == "wake": ...
    elif label in self._wake_cfg.interrupt_words: ...

Standalone test (no GAMA required):
    python -m wake_word.listener

Author : Vineet Machchal
"""

from .config import WakeWordConfig, load_wake_word_config
from .listener import WakeWordListener

__all__ = ["WakeWordConfig", "load_wake_word_config", "WakeWordListener"]
