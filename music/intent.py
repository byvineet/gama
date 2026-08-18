"""
music/intent.py — Music intent parser.
======================================
Parses natural-language music commands into a structured dict that the
Music Controller can act on. Keeps regex out of the rest of the engine.
"""

from __future__ import annotations

import re
from typing import Dict, Optional


class MusicIntent:
    """Normalized music command."""

    def __init__(self, action: str, query: str = "", platform: str = "",
                 artist: str = "", seconds: int = 0, level: int = 0,
                 use_context: bool = False) -> None:
        self.action = action
        self.query = query
        self.platform = platform
        self.artist = artist
        self.seconds = seconds
        self.level = level
        self.use_context = use_context

    def to_dict(self) -> Dict[str, any]:
        return {
            "action": self.action,
            "query": self.query,
            "platform": self.platform,
            "artist": self.artist,
            "seconds": self.seconds,
            "level": self.level,
            "use_context": self.use_context,
        }


class MusicIntentParser:
    """Parse music-related voice/text commands."""

    PLATFORM_ALIASES = {
        "spotify": "spotify",
        "youtube music": "youtube_music",
        "yt music": "youtube_music",
        "youtube": "youtube",
        "yt": "youtube",
        "local": "local",
        "my music": "local",
    }

    def __init__(self) -> None:
        self._last_query: str = ""

    def parse(self, text: str) -> Optional[MusicIntent]:
        text = text.strip()
        if not text:
            return None
        lowered = text.lower()

        # Transport controls
        # "current"/"this" are accepted alongside "the" as qualifiers
        # (e.g. "pause current song", "stop this track") — previously
        # only "the music/song/playback" matched, so "current song" fell
        # through every rule here, then through fast_intent's mirror of
        # these same patterns, and ended up going all the way to the
        # cloud LLM, which had nothing better to do than treat "current
        # song" as a literal track title to search for — hence "there's
        # no 'current song' playing". Recognizing it locally also means
        # it's answered by the <20ms fast-intent path instead of a full
        # network round trip, which is a big chunk of the latency this
        # was meant to fix.
        _QUALIFIER = r"(?:\s+(?:the|current|this|my)?\s*(?:music|song|track|playback))?"

        # Compound "stop/pause <current song> and play <X>" — starting a
        # new track already implies stopping whatever's playing, so this
        # collapses straight to a single "play <X>" intent instead of
        # requiring two separate commands.
        compound_match = re.search(
            r"^(?:stop|pause)\b.*?\band\s+play\s+(?P<rest>.{2,120})$", lowered
        )
        if compound_match:
            # Re-run against the original-cased text so query casing (song
            # titles) is preserved.
            rest_start = text.lower().rindex(compound_match.group("rest"))
            query = text[rest_start:].strip()
            platform, query = self._extract_platform(query)
            query = self._clean_query(query)
            if query:
                self._last_query = query
                return MusicIntent("play", query=query, platform=platform)

        if re.search(rf"^(pause|stop){_QUALIFIER}$", lowered):
            return MusicIntent("pause")
        if re.search(r"^(pause|stop)\s+it$", lowered):
            return MusicIntent("pause")
        if re.search(rf"^(resume|continue|unpause){_QUALIFIER}$", lowered):
            return MusicIntent("resume")
        if re.search(r"^(resume|continue|play)\s+it$", lowered):
            return MusicIntent("resume")
        if re.search(r"^(next|skip)(\s+(song|track|this|current))?$", lowered):
            return MusicIntent("next")
        if re.search(r"^(previous|prev|go\s+back|last\s+song)$", lowered):
            return MusicIntent("previous")
        if re.search(rf"^(restart|start\s+over|replay){_QUALIFIER}$", lowered):
            return MusicIntent("restart")
        if re.search(r"^(shuffle)(\s+(the\s+)?(music|queue|songs))?$", lowered):
            return MusicIntent("shuffle")
        if re.search(r"^(repeat)(\s+(this\s+)?(song|track))?$", lowered):
            return MusicIntent("repeat", seconds=1)  # repeat one
        if re.search(r"^(repeat\s+all|repeat\s+queue|repeat\s+on)$", lowered):
            return MusicIntent("repeat", seconds=2)  # repeat all
        if re.search(r"^(repeat\s+off)$", lowered):
            return MusicIntent("repeat", seconds=0)
        if re.search(r"^(volume\s+up|turn\s+(the\s+)?volume\s+up|louder)$", lowered):
            return MusicIntent("volume", level=+10)
        if re.search(r"^(volume\s+down|turn\s+(the\s+)?volume\s+down|quieter)$", lowered):
            return MusicIntent("volume", level=-10)
        if re.search(r"^(mute)$", lowered):
            return MusicIntent("mute")
        if re.search(r"^(unmute)$", lowered):
            return MusicIntent("unmute")
        if re.search(r"^(what'?s\s+playing|what\s+is\s+playing|what\s+are\s+you\s+playing|now\s+playing)$", lowered):
            return MusicIntent("now_playing")
        if re.search(r"^(play\s+similar|play\s+more\s+like\s+this|similar\s+songs)$", lowered):
            return MusicIntent("play_similar")

        # Seek
        seek_match = re.search(
            r"seek\s+(forward|backward|back|ahead)?\s*(?P<secs>\d+)\s*(seconds?|secs?|s)?",
            lowered,
        )
        if not seek_match:
            seek_match = re.search(
                r"(?:go\s+)?(?P<dir>forward|backward|back|ahead)\s+(?P<secs>\d+)\s*(seconds?|secs?|s)?",
                lowered,
            )
        if seek_match:
            secs = int(seek_match.group("secs"))
            if "back" in lowered or "backward" in lowered:
                secs = -secs
            return MusicIntent("seek", seconds=secs)

        # Volume set
        vol_match = re.search(r"volume\s+(?:to\s+)?(\d{1,3})", lowered)
        if vol_match:
            return MusicIntent("volume", level=int(vol_match.group(1)))

        # Context-aware play commands
        if re.search(r"^(?:play|spotify)\s+(?:this|that|it)(?:\s+(?:song|track|music))?$", lowered):
            return MusicIntent("play", query="__context__", use_context=True)

        # Play commands
        play_match = re.search(
            r"^(?:play|spotify)\s+(?P<rest>.{2,120})$",
            text,
            re.IGNORECASE,
        )
        if play_match:
            query = play_match.group("rest").strip()
            platform, query = self._extract_platform(query)
            query = self._clean_query(query)
            if query and len(query) > 1 and query.lower() not in ("song", "track", "music", "the", "a"):
                self._last_query = query
                return MusicIntent("play", query=query, platform=platform)

        return None

    def _extract_platform(self, query: str) -> tuple:
        lowered = query.lower()
        for alias, platform in sorted(self.PLATFORM_ALIASES.items(), key=lambda x: -len(x[0])):
            suffix = f" on {alias}"
            if lowered.endswith(suffix):
                return platform, query[: -len(suffix)].strip()
            if lowered.startswith(f"{alias} "):
                return platform, query[len(alias):].strip()
        return "", query

    def _clean_query(self, query: str) -> str:
        query = query.strip().strip("""'.,!?""")
        # Remove leading "the song / the track / my song"
        query = re.sub(r"^(the|my|this|a)\s+(song|track|music)\s+", "", query, flags=re.I)
        query = re.sub(r"^(song|track|music)\s+", "", query, flags=re.I)
        # Remove trailing "song / track / music"
        query = re.sub(r"\s+(song|track|music)$", "", query, flags=re.I)
        return query.strip()


__all__ = ["MusicIntent", "MusicIntentParser"]
