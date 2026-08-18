"""
music/providers/local.py — Local Music Provider.
=================================================
Indexes the user's Music folder and plays matching files via the
system's default media player. Lightweight, no external services.

Verification
------------
`os.startfile()` hands the file off to whatever the user's default
player is (Windows Media Player, Groove, VLC, foobar2000, ...) — we
don't control which app opens or whether it reports into Windows'
Global System Media Transport Controls (GSMTC), so a single check is
not reliable enough. `is_playing()` therefore tries several
independent, best-effort signals and accepts the first one that comes
back positive:

    1. GSMTC session          — any active session whose title/artist
                                 matches the file we launched.
    2. Window title            — a visible top-level window whose title
                                 contains the track's file name or a
                                 known player's name.
    3. Live audio peak (pycaw) — some process is actually producing
                                 non-silent audio output right now.
    4. Open file handle        — a running process still has the
                                 launched file open (psutil).

Each check is independently optional (missing dependency / API
failure just skips that check) so this never raises, and never
requires all four signals to agree — real playback almost always
trips at least one of them within a couple of seconds.
"""

from __future__ import annotations

import difflib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from music.providers.base import BaseProvider, TrackInfo

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"
_MUSIC_EXTENSIONS = (".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma")
_DEFAULT_MUSIC_PATHS = [Path.home() / "Music"]

_KNOWN_PLAYER_NAMES = (
    "windows media player", "groove music", "vlc media player", "vlc",
    "foobar2000", "musicbee", "winamp", "media player",
)


class LocalMusicProvider(BaseProvider):
    """Find and play local music files."""

    name = "local"

    def __init__(self, folders: Optional[List[Path]] = None) -> None:
        self._folders = folders or [p for p in _DEFAULT_MUSIC_PATHS if p.exists()]
        self._lock = threading.Lock()
        self._index: List[Tuple[str, Path]] = []  # (normalized, path)
        self._index_built = False
        self._last_query: str = ""
        self._last_path: Optional[Path] = None
        self._last_launch_ts: float = 0.0

    def is_available(self) -> bool:
        return bool(self._folders)

    def _build_index(self) -> None:
        if self._index_built:
            return
        with self._lock:
            if self._index_built:
                return
            idx: List[Tuple[str, Path]] = []
            for folder in self._folders:
                try:
                    for ext in _MUSIC_EXTENSIONS:
                        for p in folder.rglob(f"*{ext}"):
                            norm = self._normalize(p.stem)
                            idx.append((norm, p))
                            # also index with parent folder name for context
                            parent = p.parent.name
                            if parent and parent.lower() != "music":
                                idx.append((self._normalize(f"{parent} {p.stem}"), p))
                except Exception:
                    logger.debug("local music indexing failed for %s", folder, exc_info=True)
            self._index = idx
            self._index_built = True
            logger.info("[LocalMusic] Indexed %d tracks", len(idx))

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.lower().replace("_", " ").replace("-", " ").split())

    def _find_best(self, query: str) -> Optional[Path]:
        self._build_index()
        if not self._index:
            return None
        norm_q = self._normalize(query)
        matches = difflib.get_close_matches(norm_q, [i[0] for i in self._index], n=5, cutoff=0.55)
        if not matches:
            return None
        # Return the path of the highest-scoring match.
        match_map = {norm: path for norm, path in self._index}
        return match_map.get(matches[0])

    def play(self, query: str) -> bool:
        path = self._find_best(query)
        if not path or not path.exists():
            logger.info("[LocalMusic] No local match for '%s'", query)
            return False
        try:
            if _IS_WINDOWS:
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            self._last_query = query
            self._last_path = path
            self._last_launch_ts = time.monotonic()
            logger.info("[LocalMusic] Playing %s", path)
            return True
        except Exception:
            logger.debug("[LocalMusic] play failed", exc_info=True)
            return False

    def play_url(self, url: str) -> bool:
        return False

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def stop(self) -> bool:
        return False

    def next(self) -> bool:
        return False

    def previous(self) -> bool:
        return False

    def seek(self, seconds: float) -> bool:
        return False

    def set_volume(self, percent: int) -> bool:
        return False

    def current_track(self) -> Optional[TrackInfo]:
        if not self._last_path:
            return None
        # NOTE: deliberately NOT calling self.is_playing() here. That method
        # runs a chain of best-effort checks — including a psutil
        # process_iter(["open_files"]) scan over every running process —
        # which can take several seconds to tens of seconds depending on
        # how many processes are running. current_track() is called from
        # MusicController.play() on the response hot path (to build the
        # "Playing X on Local." confirmation), so running that scan here
        # was adding many extra seconds of latency to every single local
        # playback command (see SLOW STAGE warnings for music_engine).
        # Real verification already happens off the hot path via
        # MusicController._verify_playback_async(); a track we *just*
        # launched is playing until proven otherwise, so assume True here
        # and let the background verifier correct is_playing if it wasn't.
        just_launched = (time.monotonic() - self._last_launch_ts) < 8.0
        return TrackInfo(
            title=self._last_path.stem,
            source=self.name,
            url=str(self._last_path),
            is_playing=just_launched or self.is_playing(),
        )

    def is_playing(self) -> bool:
        """Best-effort playback verification across several independent
        signals (see module docstring). Returns True on the first one
        that positively confirms playback; False if none do."""
        if not _IS_WINDOWS or not self._last_path:
            return False

        stem_norm = self._normalize(self._last_path.stem)

        checks = (
            self._check_gsmtc,
            self._check_window_title,
            self._check_audio_peak,
            self._check_open_handle,
        )
        for check in checks:
            try:
                result = check(stem_norm)
            except Exception:
                logger.debug("[LocalMusic] verification check %s raised", check.__name__,
                             exc_info=True)
                result = None
            if result is True:
                logger.debug("[LocalMusic] playback verified via %s", check.__name__)
                return True
        return False

    # -- Individual verification signals -------------------------------
    # Each returns True (confirmed), False (confirmed NOT playing), or
    # None (this signal is unavailable / inconclusive) so callers can
    # tell "checked and no" apart from "couldn't check".

    def _check_gsmtc(self, stem_norm: str) -> Optional[bool]:
        try:
            from music.media_session import MediaSessionManager
        except Exception:
            return None
        media = MediaSessionManager()
        if not media.is_available():
            return None
        track = media.current_track()
        if not track or not track.is_playing:
            return None
        combined = self._normalize(f"{track.title} {track.artist}")
        if stem_norm and (stem_norm in combined or combined in stem_norm
                           or difflib.SequenceMatcher(None, stem_norm, combined).ratio() > 0.6):
            return True
        return None

    def _check_window_title(self, stem_norm: str) -> Optional[bool]:
        try:
            import win32gui
        except Exception:
            return None

        titles: List[str] = []

        def _cb(hwnd, _acc):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    titles.append(title.lower())
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            return None

        for title in titles:
            norm_title = self._normalize(title)
            if stem_norm and stem_norm in norm_title:
                return True
            if any(name in title for name in _KNOWN_PLAYER_NAMES):
                # A known player window is open at all — soft positive
                # once we've given it a moment to actually start audio.
                if time.monotonic() - self._last_launch_ts > 1.0:
                    return True
        return None

    def _check_audio_peak(self, stem_norm: str) -> Optional[bool]:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            from comtypes import cast, POINTER
            from utils.audio_endpoint import ensure_com_initialized
            ensure_com_initialized()
        except Exception:
            return None
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            return None

        for session in sessions:
            try:
                proc = session.Process
                if proc is None:
                    continue
                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                peak = meter.GetPeakValue()
                if peak and peak > 0.001:
                    return True
            except Exception:
                continue
        return None

    def _check_open_handle(self, stem_norm: str) -> Optional[bool]:
        if not self._last_path:
            return None
        try:
            import psutil
        except Exception:
            return None
        target = str(self._last_path).lower()
        try:
            for proc in psutil.process_iter(["open_files"]):
                try:
                    for f in (proc.info.get("open_files") or []):
                        if f.path.lower() == target:
                            return True
                except Exception:
                    continue
        except Exception:
            return None
        return None
