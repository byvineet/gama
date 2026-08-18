"""
Gama - User State Manager
=========================
Central state coordinator tracking whether the user is in a class, meeting, gaming,
sleeping, in deep focus/DND, or actively working. Controls proactivity throttling.

Author : Vineet Machchal
"""

from __future__ import annotations

import enum
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("gama.state.user_state")


class UserState(str, enum.Enum):
    IDLE = "IDLE"
    ACTIVE_WORKING = "ACTIVE_WORKING"
    IN_MEETING = "IN_MEETING"
    IN_CLASS = "IN_CLASS"
    GAMING = "GAMING"
    DND_FOCUS = "DND_FOCUS"
    SLEEPING = "SLEEPING"
    AWAY_GUARDED = "AWAY_GUARDED"


class PriorityLevel(int, enum.Enum):
    P0_EMERGENCY = 0   # Guard mode breaches, safety, critical errors
    P1_URGENT = 1      # Immediate meetings (<2m), urgent direct reminders
    P2_NORMAL = 2      # Standard reminders, goal deadlines, class alerts
    P3_PROACTIVE = 3   # Proactive recommendations, routine suggestions


class UserStateManager:
    """Thread-safe manager for shared User State and proactivity throttling."""

    def __init__(self):
        self._lock = threading.RLock()
        self._current_state: UserState = UserState.IDLE
        self._state_source: str = "default"
        self._expires_at: Optional[datetime] = None
        self._metadata: Dict[str, Any] = {}

    def set_state(
        self,
        state: UserState,
        source: str = "manual",
        duration_minutes: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UserState:
        with self._lock:
            self._current_state = state
            self._state_source = source
            if duration_minutes:
                self._expires_at = datetime.now() + timedelta(minutes=duration_minutes)
            else:
                self._expires_at = None
            self._metadata = metadata or {}
            logger.info(f"[UserStateManager] State updated to {state.value} by {source}")
            return self._current_state

    def get_state(self) -> UserState:
        with self._lock:
            if self._expires_at and datetime.now() > self._expires_at:
                logger.info(f"[UserStateManager] State {self._current_state.value} expired. Reverting to IDLE.")
                self._current_state = UserState.IDLE
                self._expires_at = None
            return self._current_state

    def is_audio_allowed(self, priority: PriorityLevel) -> bool:
        """Return True if TTS/speech audio is permitted for this priority level in the current state."""
        state = self.get_state()
        if priority == PriorityLevel.P0_EMERGENCY:
            return True
        if state in (UserState.IN_CLASS, UserState.IN_MEETING, UserState.SLEEPING, UserState.GAMING, UserState.DND_FOCUS):
            # No vocal interruptions for P2/P3 in class/meeting/gaming/DND/sleeping
            return False
        return True

    def is_visual_allowed(self, priority: PriorityLevel) -> bool:
        """Return True if UI toast/banner notifications are permitted."""
        state = self.get_state()
        if priority == PriorityLevel.P0_EMERGENCY:
            return True
        if state == UserState.SLEEPING:
            return False
        return True


# Global singleton instance
user_state_manager = UserStateManager()
