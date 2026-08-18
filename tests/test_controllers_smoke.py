"""Smoke tests for Phase-1 controller modules (no live audio/session)."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace


def test_sleep_controller_observe():
    from core.sleep_controller import SleepController

    events = []
    asst = SimpleNamespace(
        _awake=True,
        _wake_verifying=True,
        _auto_sleep_task=None,
        _loop=None,
        session=None,
        _observe_pending_request="x",
        _cancel_auto_sleep=lambda: events.append("cancel_sleep"),
        _session_mgr=SimpleNamespace(end_session=lambda r: events.append(("end", r))),
        _runtime=SimpleNamespace(force_observe=lambda r: events.append(("obs", r))),
        _sync_clap_arm=lambda: events.append("clap"),
        _flush_playback=lambda reason="": events.append(("flush", reason)),
        ui=SimpleNamespace(
            set_state=lambda s: events.append(("state", s)),
            emit_event=lambda *a, **k: None,
            write_log=lambda h: events.append("log"),
        ),
        _wake_cfg=SimpleNamespace(wake_phrase="gama"),
    )
    ctl = SleepController(asst)
    ctl._enter_observe_mode("test")
    assert asst._awake is False
    assert ("obs", "test") in events


def test_barge_in_hard_stop_noop():
    from core.barge_in_controller import BargeInController

    asst = SimpleNamespace(
        _live_out_stream=None,
        _live_out_stream_lock=threading.Lock(),
    )
    BargeInController(asst)._hard_stop_speaker()


def test_audio_controller_flush():
    from core.audio_controller import AudioController

    asst = SimpleNamespace(
        _set_speaking=lambda *a, **k: None,
        _hard_stop_speaker=lambda: None,
        audio_in_queue=queue.Queue(),
    )
    asst.audio_in_queue.put(b"x")
    ctl = AudioController(asst)
    ctl.flush_playback("test")
    assert asst.audio_in_queue.empty()
