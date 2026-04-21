"""Tests for recovery_announcer (Slice 3).

Headless-safe: every test injects a capture-list speaker. The macOS
``say`` binding is never imported.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from backend.core.ouroboros.governance.recovery_advisor import (
    FailureContext,
    STOP_COST_CAP,
    advise,
)
from backend.core.ouroboros.governance.recovery_announcer import (
    RECOVERY_ANNOUNCER_SCHEMA_VERSION,
    RecoveryAnnouncer,
    get_default_announcer,
    is_voice_live,
    narrator_enabled,
    recovery_voice_enabled,
    reset_default_announcer,
    set_default_announcer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    reset_default_announcer()
    # Clean any env that would leak between tests.
    for key in (
        "OUROBOROS_NARRATOR_ENABLED",
        "JARVIS_RECOVERY_VOICE_ENABLED",
        "JARVIS_RECOVERY_VOICE_MIN_GAP_S",
    ):
        os.environ.pop(key, None)
    yield
    reset_default_announcer()
    for key in (
        "OUROBOROS_NARRATOR_ENABLED",
        "JARVIS_RECOVERY_VOICE_ENABLED",
        "JARVIS_RECOVERY_VOICE_MIN_GAP_S",
    ):
        os.environ.pop(key, None)


def _enable_voice(monkeypatch):
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "true")


class _CaptureSpeaker:
    def __init__(self):
        self.calls = []

    async def __call__(self, text, voice="Karen"):
        self.calls.append((text, voice))
        return True


def _plan(op_id: str = "op-1"):
    return advise(FailureContext(
        op_id=op_id,
        stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80,
        cost_cap_usd=0.50,
    ))


# ===========================================================================
# Schema + env flags
# ===========================================================================


def test_schema_version_pinned():
    assert RECOVERY_ANNOUNCER_SCHEMA_VERSION == "recovery_announcer.v1"


def test_narrator_enabled_default_true():
    assert narrator_enabled() is True


def test_recovery_voice_enabled_default_false():
    """Opt-in posture — recovery narration is OFF unless operator asks."""
    assert recovery_voice_enabled() is False


def test_is_voice_live_requires_both_flags(monkeypatch):
    # Master off → not live regardless of sub-switch
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "true")
    assert is_voice_live() is False
    # Master on + sub-switch off → not live
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "false")
    assert is_voice_live() is False
    # Both on → live
    _enable_voice(monkeypatch)
    assert is_voice_live() is True


def test_voice_flag_truthy_synonyms(monkeypatch):
    """Accept 1/yes/on alongside true for both env flags."""
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "yes")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "on")
    assert is_voice_live() is True


# ===========================================================================
# announce() gated by env flags
# ===========================================================================


def test_announce_no_op_when_voice_disabled():
    speaker = _CaptureSpeaker()
    ann = RecoveryAnnouncer(speaker=speaker)
    result = ann.announce(_plan())
    assert result is False
    stats = ann.stats()
    assert stats["queued"] == 0
    assert stats["suppressed"] == 1


def test_announce_enqueues_when_voice_live(monkeypatch):
    _enable_voice(monkeypatch)
    speaker = _CaptureSpeaker()
    ann = RecoveryAnnouncer(speaker=speaker)
    assert ann.announce(_plan()) is True
    assert ann.stats()["queued"] == 1


def test_announce_skips_empty_plan(monkeypatch):
    _enable_voice(monkeypatch)
    from backend.core.ouroboros.governance.recovery_advisor import (
        RecoveryPlan,
    )
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    empty = RecoveryPlan(op_id="op-1", failure_summary="")
    assert ann.announce(empty) is False


def test_announce_skips_none_plan(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    assert ann.announce(None) is False  # type: ignore[arg-type]


# ===========================================================================
# Idempotency
# ===========================================================================


def test_announce_same_plan_twice_only_fires_once(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    plan = _plan()
    assert ann.announce(plan) is True
    assert ann.announce(plan) is False
    stats = ann.stats()
    assert stats["queued"] == 1
    assert stats["suppressed"] == 1


def test_announce_different_ops_not_deduped(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    assert ann.announce(_plan("op-a")) is True
    assert ann.announce(_plan("op-b")) is True
    assert ann.stats()["queued"] == 2


def test_idempotency_cap_evicts_oldest(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker(), idempotency_cap=16)
    # Enough unique plans to push out the earliest key
    for i in range(20):
        ann.announce(_plan(f"op-{i}"))
    # The first plan (op-0) should have fallen out of the set, so it
    # re-announces cleanly.
    assert ann.announce(_plan("op-0")) is True


# ===========================================================================
# Drop-oldest queue behavior
# ===========================================================================


def test_queue_sheds_oldest_when_full(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker(), queue_maxsize=2)
    assert ann.announce(_plan("op-1")) is True
    assert ann.announce(_plan("op-2")) is True
    # Third push fills beyond cap → drop oldest
    assert ann.announce(_plan("op-3")) is True
    stats = ann.stats()
    assert stats["shed"] == 1
    assert stats["queued"] == 2


# ===========================================================================
# Drain through injected speaker
# ===========================================================================


def test_drain_once_speaks_text(monkeypatch):
    _enable_voice(monkeypatch)
    speaker = _CaptureSpeaker()
    ann = RecoveryAnnouncer(speaker=speaker)
    ann.announce(_plan())

    result = asyncio.new_event_loop().run_until_complete(
        ann.drain_once_for_test(),
    )
    assert result is not None
    key, text = result
    assert speaker.calls, "speaker should have been called"
    spoken_text, spoken_voice = speaker.calls[0]
    assert "three things" in spoken_text.lower()
    assert spoken_voice == "Karen"


def test_drain_without_speaker_returns_none():
    # No speaker + empty queue
    ann = RecoveryAnnouncer()
    result = asyncio.new_event_loop().run_until_complete(
        ann.drain_once_for_test(),
    )
    assert result is None


def test_drain_empty_queue_returns_none(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    result = asyncio.new_event_loop().run_until_complete(
        ann.drain_once_for_test(),
    )
    assert result is None


# ===========================================================================
# announce_text — REPL pathway
# ===========================================================================


def test_announce_text_accepts_prerendered_text(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    assert ann.announce_text("manual", "Try resuming the op.") is True
    assert ann.stats()["queued"] == 1


def test_announce_text_blocked_when_voice_disabled():
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    assert ann.announce_text("manual", "hello") is False


def test_announce_text_empty_returns_false(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    assert ann.announce_text("manual", "") is False


# ===========================================================================
# Speaker exception isolation
# ===========================================================================


def test_speaker_exception_does_not_crash(monkeypatch):
    _enable_voice(monkeypatch)

    async def _explode(text, voice="Karen"):
        raise RuntimeError("audio stack broke")

    ann = RecoveryAnnouncer(speaker=_explode)
    ann.announce(_plan())
    # Drain — must not raise
    result = asyncio.new_event_loop().run_until_complete(
        ann.drain_once_for_test(),
    )
    assert result is not None


# ===========================================================================
# reset() + singleton
# ===========================================================================


def test_reset_clears_state(monkeypatch):
    _enable_voice(monkeypatch)
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker())
    ann.announce(_plan())
    assert ann.stats()["queued"] == 1
    ann.reset()
    stats = ann.stats()
    assert stats["queued"] == 0
    assert stats["spoken"] == 0


def test_singleton_returns_same_instance():
    a = get_default_announcer()
    b = get_default_announcer()
    assert a is b


def test_set_default_announcer_replaces_singleton():
    custom = RecoveryAnnouncer()
    set_default_announcer(custom)
    assert get_default_announcer() is custom
    reset_default_announcer()
    # After reset, a fresh singleton
    assert get_default_announcer() is not custom


# ===========================================================================
# Stats shape
# ===========================================================================


def test_stats_shape():
    ann = RecoveryAnnouncer(speaker=_CaptureSpeaker(), queue_maxsize=5)
    stats = ann.stats()
    for key in (
        "schema_version", "queued", "queue_maxsize",
        "spoken", "shed", "suppressed", "is_live", "voice",
        "min_gap_s", "idempotency_seen",
    ):
        assert key in stats, f"missing key: {key}"
    assert stats["queue_maxsize"] == 5
    assert stats["voice"] == "Karen"


# ===========================================================================
# No audio imports on bare construction
# ===========================================================================


def test_construction_does_not_import_macos_say():
    """Pin: creating the announcer must not pull in the audio stack.

    This ensures sandbox / CI runs stay silent-by-construction.
    """
    import sys
    before = set(sys.modules.keys())
    _ = RecoveryAnnouncer()
    after = set(sys.modules.keys())
    new = after - before
    forbidden = {
        "backend.core.supervisor.unified_voice_orchestrator",
    }
    leaked = [m for m in new if m in forbidden]
    assert leaked == [], f"announcer leaked audio imports: {leaked}"
