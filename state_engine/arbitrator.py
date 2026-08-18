"""
Gama - Notification & Interruption Arbitrator
==============================================
Traffic cop for all background notifications, reminders, goal check-ins, and proactive advice.
Prevents overlapping speech, arbitrates priority, batches simultaneous alerts, and manages
the Post-Class / Post-Meeting debrief queue.

Author : Vineet Machchal
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from state_engine.user_state import PriorityLevel, UserState, user_state_manager
from actions.desktop_notify import notify

logger = logging.getLogger("gama.state.arbitrator")


@dataclass
class NotificationItem:
    id: str
    title: str
    message: str
    priority: PriorityLevel
    category: str
    speak: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    data: Dict[str, Any] = field(default_factory=dict)


class NotificationArbitrator:
    """Interruption & priority arbitration engine."""

    def __init__(self):
        self._lock = threading.RLock()
        self._speech_lock = threading.Lock()
        self._last_speech_time: float = 0.0
        self._speech_cooldown_seconds: float = 4.0
        self._debrief_queue: List[NotificationItem] = []
        self._pending_batch: List[NotificationItem] = []
        self._batch_timer: Optional[threading.Timer] = None
        self._tts_speak_fn: Optional[Callable[[str], None]] = None

    def register_tts_speaker(self, speak_fn: Callable[[str], None]) -> None:
        """Register the system TTS speak function (e.g., voice_recognition or media_controller speaker)."""
        self._tts_speak_fn = speak_fn

    def dispatch(
        self,
        title: str,
        message: str,
        priority: PriorityLevel = PriorityLevel.P2_NORMAL,
        category: str = "general",
        speak: bool = True,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Submit a notification for arbitration and dispatching."""
        item = NotificationItem(
            id=f"{category}_{int(time.time()*1000)}",
            title=title,
            message=message,
            priority=priority,
            category=category,
            speak=speak,
            data=data or {},
        )

        with self._lock:
            state = user_state_manager.get_state()
            logger.info(f"[Arbitrator] Dispatching notification '{title}' (P{priority.value}) during state={state.value}")

            # 1. Handle Silent Debrief Queueing (IN_CLASS / IN_MEETING / SLEEPING)
            if state in (UserState.IN_CLASS, UserState.IN_MEETING, UserState.SLEEPING):
                if priority > PriorityLevel.P1_URGENT:
                    self._debrief_queue.append(item)
                    logger.info(f"[Arbitrator] Deferred P{priority.value} notification to debrief queue. (Total queued: {len(self._debrief_queue)})")
                    # Visual desktop notification only if allowed
                    if user_state_manager.is_visual_allowed(priority):
                        notify(f"[{state.value}] {title}", message)
                    return False

            # 2. Priority Preemption & Direct Delivery
            visual_ok = user_state_manager.is_visual_allowed(priority)
            audio_ok = speak and user_state_manager.is_audio_allowed(priority)

            if visual_ok:
                notify(title, message)

            if audio_ok:
                self._speak_with_lock(message)

            return True

    def _speak_with_lock(self, text: str) -> None:
        """Ensure TTS speech is non-overlapping and respects speech cooldown."""
        def _speak_worker():
            with self._speech_lock:
                now = time.time()
                elapsed = now - self._last_speech_time
                if elapsed < self._speech_cooldown_seconds:
                    time.sleep(self._speech_cooldown_seconds - elapsed)
                
                if self._tts_speak_fn:
                    try:
                        self._tts_speak_fn(text)
                    except Exception as e:
                        logger.error(f"[Arbitrator] Error speaking text: {e}")
                else:
                    logger.info(f"[Arbitrator TTS Output]: {text}")
                
                self._last_speech_time = time.time()

        threading.Thread(target=_speak_worker, daemon=True).start()

    def get_debrief_summary(self) -> Optional[str]:
        """Return the current deferred debrief summary text without clearing."""
        with self._lock:
            if not self._debrief_queue:
                return None
            count = len(self._debrief_queue)
            categories = list({item.category for item in self._debrief_queue})
            return f"You have {count} updates deferred from your recent session across {', '.join(categories)}."

    def flush_debrief_queue(self) -> Optional[str]:
        """Deliver a consolidated summary of all deferred notifications when class/meeting ends."""
        with self._lock:
            if not self._debrief_queue:
                return None

            count = len(self._debrief_queue)
            categories = list({item.category for item in self._debrief_queue})
            summary_msg = f"You have {count} updates deferred from your recent session across {', '.join(categories)}."
            
            # Reset queue
            self._debrief_queue.clear()

            # Speak summary if state is active
            if user_state_manager.is_audio_allowed(PriorityLevel.P2_NORMAL):
                self._speak_with_lock(summary_msg)

            notify("Session Debrief", summary_msg)
            return summary_msg


# Global singleton instance
arbitrator = NotificationArbitrator()
