"""
core/protocols/parser.py — Natural language -> Protocol steps
================================================================================
Turns a free-text description like:

    "open Chrome, then open Spotify, wait 2 seconds, then play music"

into an ordered list of ProtocolStep objects. This is intentionally a fast,
rule-based parser (no LLM round trip needed for common phrasing) so protocol
creation feels instant; anything unrecognized falls back to a TOOL/AI_PROMPT
step so nothing the user says is silently dropped.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core.protocols.models import ActionType, OnFailureStrategy, PermissionLevel, Protocol, ProtocolStep
from core.protocols.registry import normalize_identifier

_QUOTE_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')


def _mask_quoted_spans(text: str) -> Tuple[str, Dict[str, str]]:
    """Replace quoted substrings with placeholders so splitting on commas/
    'then' doesn't break apart a quoted phrase like 'search "cats and dogs"'."""
    originals: Dict[str, str] = {}
    counter = [0]

    def _stash(m: "re.Match[str]") -> str:
        key = f"__Q{counter[0]}__"
        counter[0] += 1
        originals[key] = m.group(1) if m.group(1) is not None else m.group(2)
        return key

    masked = _QUOTE_RE.sub(_stash, text)
    return masked, originals


def _unmask(chunk: str, originals: Dict[str, str]) -> str:
    for key, val in originals.items():
        chunk = chunk.replace(key, val)
    return chunk


# Ordered rule table: (regex, action_type, target_group, extra_params_fn)
_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:open|launch|start)\s+(?:the\s+)?(?:app\s+)?(.+?)\s+folder$", re.I), ActionType.OPEN_FOLDER.value),
    (re.compile(r"^open\s+folder\s+(.+)$", re.I), ActionType.OPEN_FOLDER.value),
    (re.compile(r"^open\s+file\s+(.+)$", re.I), ActionType.OPEN_FILE.value),
    (re.compile(r"^(?:close|quit|exit)\s+(?:the\s+)?(?:app\s+)?(.+)$", re.I), ActionType.CLOSE_APP.value),
    (re.compile(r"^(?:open|launch|start)\s+(?:the\s+)?(?:app\s+)?(.+)$", re.I), ActionType.OPEN_APP.value),
    (re.compile(r"^(?:run|execute)\s+(?:command|terminal)\s+(.+)$", re.I), ActionType.TERMINAL.value),
    (re.compile(r"^(?:run|execute)\s+protocol\s+(.+)$", re.I), ActionType.CALL_PROTOCOL.value),
    (re.compile(r"^(?:search|google|web\s*search)\s+(?:for\s+)?(.+)$", re.I), ActionType.WEB_SEARCH.value),
    (re.compile(r"^(?:go\s+to|navigate\s+to|open\s+(?:website|url|browser))\s+(.+)$", re.I), ActionType.BROWSER.value),
    (re.compile(r"^play\s+(?:music|song)?\s*(.*)$", re.I), ActionType.MEDIA_PLAY.value),
    (re.compile(r"^pause\s+(?:music|media)?$", re.I), ActionType.MEDIA_PAUSE.value),
    (re.compile(r"^(?:set\s+)?volume\s+(?:to\s+)?(\d+)%?$", re.I), ActionType.VOLUME.value),
    (re.compile(r"^(?:set\s+)?brightness\s+(?:to\s+)?(\d+)%?$", re.I), ActionType.BRIGHTNESS.value),
    (re.compile(r"^notify\s+(?:me\s+)?(?:that\s+)?(.+)$", re.I), ActionType.NOTIFICATION.value),
    (re.compile(r"^copy\s+(.+)\s+to\s+clipboard$", re.I), ActionType.CLIPBOARD.value),
    (re.compile(r"^(?:write|type)\s+(.+)$", re.I), ActionType.TYPE_TEXT.value),
    (re.compile(r"^wait\s+(?:for\s+)?(\d+(?:\.\d+)?)\s*(?:sec|second|seconds|s)?$", re.I), ActionType.WAIT.value),
    (re.compile(r"^(?:wait\s+until|wait\s+for)\s+(.+?)\s+(?:starts|is\s+running)$", re.I), ActionType.WAIT_PROCESS.value),
    (re.compile(r"^ask\s+(?:me\s+)?(.+)$", re.I), ActionType.ASK_USER.value),
    (re.compile(r"^(?:speak|say|announce)\s+(.+)$", re.I), ActionType.SPEAK.value),
    (re.compile(r"^(?:ai|ask\s+ai|prompt)\s*[:\-]?\s*(.+)$", re.I), ActionType.AI_PROMPT.value),
    (re.compile(r"^press\s+(.+)$", re.I), ActionType.KEYBOARD.value),
]

_ON_FAILURE_HINTS = {
    "retry": OnFailureStrategy.RETRY.value,
    "skip if it fails": OnFailureStrategy.SKIP.value,
    "skip on failure": OnFailureStrategy.SKIP.value,
    "ask if it fails": OnFailureStrategy.ASK_USER.value,
}


def _clean_quoted(val: str) -> str:
    return val.strip().strip('"').strip("'").strip()


class ProtocolParser:
    """Stateless helpers for turning free text into structured steps."""

    @staticmethod
    def parse_natural_language_steps(text: str) -> List[ProtocolStep]:
        if not text or not text.strip():
            return []

        masked, originals = _mask_quoted_spans(text)
        # Split on commas, " then ", " and then ", newlines, or " -> ".
        chunks = re.split(r",|\bthen\b|\band then\b|->|\n", masked, flags=re.I)
        chunks = [c.strip() for c in chunks if c.strip()]

        steps: List[ProtocolStep] = []
        order = 0
        for raw_chunk in chunks:
            chunk = _unmask(raw_chunk, originals)
            # Support "in parallel: A, B" style groupings inline, if present.
            parallel_group = None
            group_match = re.match(r"^\(?parallel\)?\s*[:\-]?\s*(.+)$", chunk, re.I)
            if group_match:
                chunk = group_match.group(1)
                parallel_group = "group_1"

            step = ProtocolParser._parse_single_chunk(chunk)
            if step is None:
                continue
            step.order = order
            step.parallel_group = parallel_group
            order += 1
            steps.append(step)

        return steps

    @staticmethod
    def _parse_single_chunk(chunk: str) -> Optional[ProtocolStep]:
        chunk = chunk.strip()
        if not chunk:
            return None

        for pattern, action_type in _RULES:
            m = pattern.match(chunk)
            if not m:
                continue
            target = _clean_quoted(m.group(1)) if m.groups() else ""
            params: Dict[str, Any] = {}

            if action_type == ActionType.WAIT.value:
                try:
                    params["seconds"] = float(target)
                except ValueError:
                    params["seconds"] = 1.0
                target = ""
            elif action_type in (ActionType.VOLUME.value, ActionType.BRIGHTNESS.value):
                params["level"] = int(target) if target.isdigit() else 50
            elif action_type == ActionType.CALL_PROTOCOL.value:
                num_id, slug = normalize_identifier(target)
                params["identifier"] = str(num_id) if num_id is not None else slug or target

            return ProtocolStep(action_type=action_type, target=target, params=params)

        # Nothing matched: fall back to an AI_PROMPT step so intent is never
        # silently dropped, and it's easy for the user to see & fix later.
        return ProtocolStep(action_type=ActionType.AI_PROMPT.value, target=chunk, params={"unrecognized": True})

    @staticmethod
    def build_protocol_from_prompt(
        identifier: str,
        steps_text: str,
        description: str = "",
        category: str = "General",
        confirmation_required: bool = False,
    ) -> Protocol:
        num_id, slug = normalize_identifier(identifier)
        display_name = slug.replace("_", " ").title() if slug else (
            f"Protocol {num_id}" if num_id is not None else str(identifier).title()
        )
        if not display_name.lower().endswith("protocol") and slug:
            display_name = f"{display_name} Protocol" if "protocol" not in slug else display_name

        steps = ProtocolParser.parse_natural_language_steps(steps_text)
        return Protocol(
            numeric_id=num_id,
            display_name=display_name,
            description=description or f"Runs {len(steps)} step(s): {steps_text[:120]}",
            category=category or "General",
            steps=steps,
            permission_level=(
                PermissionLevel.CONFIRM.value if confirmation_required else PermissionLevel.INSTANT.value
            ),
        )


protocol_parser = ProtocolParser()

__all__ = ["ProtocolParser", "protocol_parser"]
