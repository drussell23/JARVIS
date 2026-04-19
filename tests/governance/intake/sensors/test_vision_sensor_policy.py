"""Regression spine for the VisionSensor policy layer (Task 11).

Scope:

* FP budget ledger — disk-persisted 20-op rolling window, FP rate
  computation, auto-pause with reason ``fp_budget_exhausted`` above the
  budget threshold (default 0.3).
* Per-finding cooldown — same ``(verdict, app_id, match-set)`` within
  120s collapses; different shape emits normally.
* Chain cap — default 1; after N started ops the sensor pauses with
  reason ``chain_cap_exhausted``.
* Consecutive-failure penalty — 3 straight ``rejected``/``stale``
  outcomes → pause ``consecutive_failures`` for 300s, auto-resume when
  the deadline passes.
* Pause predicates — ``is_paused`` / ``paused`` property; short-circuits
  both ``_ingest_frame`` and ``scan_once``.
* Resume — clears pause state and chain tracker.
* Persistence — outcomes + finding cooldowns + consecutive-failure
  streak survive a restart; pause state deliberately does not (§Policy
  Layer: "fresh budget window on next session boot").

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Policy Layer.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import IntentEnvelope
from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
    OUTCOME_APPLIED_GREEN,
    OUTCOME_REJECTED,
    OUTCOME_STALE,
    OUTCOME_UNCERTAIN,
    PAUSE_REASON_CHAIN_CAP,
    PAUSE_REASON_CONSECUTIVE_FAILURES,
    PAUSE_REASON_FP_BUDGET,
    PAUSE_REASON_MANUAL,
    FrameData,
    OutcomeEntry,
    VisionSensor,
)


class _StubRouter:
    async def ingest(self, envelope: IntentEnvelope) -> str:
        return "enqueued"


def _make_sensor(
    tmp_path: Path,
    *,
    fp_budget: Optional[float] = None,
    fp_window_size: Optional[int] = None,
    finding_cooldown_s: Optional[float] = None,
    chain_max: Optional[int] = None,
    penalty_s: Optional[float] = None,
    ocr_fn=None,
) -> VisionSensor:
    return VisionSensor(
        router=_StubRouter(),
        session_id="policy-test",
        retention_root=str(tmp_path / ".jarvis" / "vision_frames"),
        frame_ttl_s=0.0,
        ledger_path=str(tmp_path / ".jarvis" / "vision_sensor_fp_ledger.json"),
        fp_budget=fp_budget,
        fp_window_size=fp_window_size,
        finding_cooldown_s=finding_cooldown_s,
        chain_max=chain_max,
        penalty_s=penalty_s,
        ocr_fn=ocr_fn,
        register_shutdown_hooks=False,
    )


def _make_frame(
    dhash: str = "0123456789abcdef",
    app_id: Optional[str] = None,
    ts: float = 1.0,
) -> FrameData:
    return FrameData(
        frame_path="/tmp/claude/latest_frame.jpg",
        dhash=dhash,
        ts=ts,
        app_id=app_id,
        window_id=None,
    )


# ---------------------------------------------------------------------------
# FP budget
# ---------------------------------------------------------------------------


def test_fp_rate_is_none_on_empty_window(tmp_path):
    sensor = _make_sensor(tmp_path)
    assert sensor.fp_rate() is None


def test_fp_rate_none_when_only_uncertain(tmp_path):
    sensor = _make_sensor(tmp_path, fp_window_size=3)
    for i in range(3):
        sensor.record_outcome(op_id=f"op-{i}", outcome=OUTCOME_UNCERTAIN)
    # Window is full but zero FP+TP — rate undefined.
    assert sensor.fp_rate() is None
    assert sensor.paused is False


def test_fp_rate_none_until_window_full(tmp_path):
    """Spec §Policy Layer: the rate only meaningfully exists once the
    rolling window is populated. Early samples never trip the budget."""
    sensor = _make_sensor(tmp_path, fp_window_size=5, fp_budget=0.1)
    # 4 of 5 slots filled with REJECTED → rate would be 1.0, but window
    # is not full yet → None → no pause.
    for i in range(4):
        sensor.record_outcome(op_id=f"f-{i}", outcome=OUTCOME_UNCERTAIN)
    assert sensor.fp_rate() is None
    # 5th fills the window, uncertain-only still None.
    sensor.record_outcome(op_id="u5", outcome=OUTCOME_UNCERTAIN)
    assert sensor.fp_rate() is None


def test_fp_rate_all_rejected_is_one(tmp_path):
    # Window size matches the outcome count so the full-window guard
    # doesn't suppress the rate.
    sensor = _make_sensor(tmp_path, fp_window_size=5, fp_budget=1.1)
    for i in range(5):
        sensor.record_outcome(op_id=f"op-{i}", outcome=OUTCOME_REJECTED)
    assert sensor.fp_rate() == 1.0


def test_fp_rate_all_applied_green_is_zero(tmp_path):
    sensor = _make_sensor(tmp_path, fp_window_size=5)
    for i in range(5):
        sensor.record_outcome(op_id=f"op-{i}", outcome=OUTCOME_APPLIED_GREEN)
    assert sensor.fp_rate() == 0.0


def test_fp_rate_stale_counted_as_fp(tmp_path):
    # 2 stale + 8 applied_green → rate = 2/10 = 0.2, below 0.3 budget.
    sensor = _make_sensor(tmp_path, fp_budget=0.3, fp_window_size=10)
    for i in range(2):
        sensor.record_outcome(op_id=f"stale-{i}", outcome=OUTCOME_STALE)
    for i in range(8):
        sensor.record_outcome(op_id=f"green-{i}", outcome=OUTCOME_APPLIED_GREEN)
    assert abs(sensor.fp_rate() - 0.2) < 1e-9
    assert sensor.paused is False


def test_fp_budget_exhaustion_pauses_sensor(tmp_path):
    # Plan test: 7 rejected + 13 applied_green → 0.35 > 0.3 → pause.
    sensor = _make_sensor(tmp_path, fp_budget=0.3, fp_window_size=20)
    for i in range(7):
        sensor.record_outcome(op_id=f"fp-{i}", outcome=OUTCOME_REJECTED)
    # Seven consecutive FPs would already trip the consecutive-failures
    # pause; interleave with TP outcomes to test the budget path in
    # isolation. Order: 1 FP, 1 TP, 1 FP, 1 TP, ... exhausts neither
    # threshold on its own but lands in the same 0.35 rate.
    sensor = _make_sensor(tmp_path, fp_budget=0.3, fp_window_size=20)
    for i in range(7):
        sensor.record_outcome(op_id=f"fp-{i}", outcome=OUTCOME_REJECTED)
        if i < 6:
            sensor.record_outcome(op_id=f"tp-{i}", outcome=OUTCOME_APPLIED_GREEN)
    # Pad to 20 entries with TPs.
    while len(sensor._outcomes) < 20:
        sensor.record_outcome(
            op_id=f"pad-{len(sensor._outcomes)}",
            outcome=OUTCOME_APPLIED_GREEN,
        )
    rate = sensor.fp_rate()
    assert rate is not None and rate > 0.3
    assert sensor.paused is True
    assert sensor.pause_reason == PAUSE_REASON_FP_BUDGET


def test_fp_budget_exhaustion_requires_manual_resume(tmp_path):
    sensor = _make_sensor(tmp_path, fp_budget=0.1, fp_window_size=5)
    # Trigger budget exhaustion cleanly (3 FP + 2 TP → 0.6 > 0.1).
    for i in range(3):
        sensor.record_outcome(op_id=f"fp-{i}", outcome=OUTCOME_REJECTED)
    # Still below chain cap and no consecutive-failure reset until we
    # record a TP — add two TPs to keep the streak below threshold but
    # leave FP rate elevated.
    sensor.record_outcome(op_id="tp-1", outcome=OUTCOME_APPLIED_GREEN)
    sensor.record_outcome(op_id="tp-2", outcome=OUTCOME_APPLIED_GREEN)
    assert sensor.paused is True
    assert sensor.pause_reason == PAUSE_REASON_FP_BUDGET
    # Even after the (fictional) penalty would have expired, the FP pause
    # is operator-gated — it doesn't auto-expire.
    sensor._pause_until_ts = time.time() - 1.0  # simulate past deadline
    assert sensor.paused is True


def test_paused_sensor_short_circuits_ingest(tmp_path):
    sensor = _make_sensor(tmp_path, ocr_fn=lambda _p: "Traceback (most recent call last):")
    # Manually pause with FP budget reason.
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    env = asyncio.run(
        sensor._ingest_frame(_make_frame(dhash="abababababababab"))
    )
    assert env is None
    assert sensor.stats.dropped_paused == 1
    assert sensor.stats.signals_emitted == 0


@pytest.mark.asyncio
async def test_paused_sensor_short_circuits_scan_once(tmp_path):
    sensor = _make_sensor(tmp_path)
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    out = await sensor.scan_once()
    assert out == []
    # Counter for paused drops incremented at scan_once gate.
    assert sensor.stats.dropped_paused == 1


# ---------------------------------------------------------------------------
# Finding cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_cooldown_drops_repeat_shape(tmp_path):
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    # First emit succeeds.
    s1 = await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="X"))
    assert s1 is not None
    # Second frame with a DIFFERENT dhash but SAME verdict+app+match-set
    # is dropped by the finding cooldown.
    s2 = await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="X"))
    assert s2 is None
    assert sensor.stats.dropped_finding_cooldown == 1


@pytest.mark.asyncio
async def test_finding_cooldown_emits_observability_log(tmp_path, caplog):
    """§8 Absolute Observability: every finding_cooldown drop MUST emit
    an INFO log line carrying the verdict + app + match-set tuple
    and a running total_drops counter.

    Silent suppression was flagged as unacceptable — this test pins
    the operator-visible log contract so a future regression where
    someone removes the INFO line turns CI red.
    """
    import logging
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    # First emit — no drop, no log.
    await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="com.apple.Terminal"))

    with caplog.at_level(logging.INFO, logger="Ouroboros.VisionSensor"):
        # Second emit with different dhash + same shape → drop + log.
        await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="com.apple.Terminal"))

    drop_lines = [r.message for r in caplog.records if "dropped finding_cooldown" in r.message]
    assert len(drop_lines) == 1, "exactly one drop → exactly one INFO line"
    msg = drop_lines[0]
    # Verdict + app + match-set tuple all present for grep rollups.
    assert "verdict=error_visible" in msg
    assert "app=com.apple.Terminal" in msg
    assert "matches=traceback" in msg
    # Running total for session-level throttling visibility.
    assert "total_drops=1" in msg


@pytest.mark.asyncio
async def test_finding_cooldown_log_total_drops_increments_per_session(tmp_path, caplog):
    """Burst scenario: N drops → N INFO lines with monotonically
    increasing ``total_drops=`` token. Proves the counter is
    session-scoped, not per-emit.
    """
    import logging
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    # Prime the cooldown.
    await sensor._ingest_frame(_make_frame(dhash="0000000000000001", app_id="X"))
    with caplog.at_level(logging.INFO, logger="Ouroboros.VisionSensor"):
        for i in range(2, 6):
            await sensor._ingest_frame(_make_frame(
                dhash=f"{i:016x}", app_id="X",
            ))

    drop_lines = [r.message for r in caplog.records if "dropped finding_cooldown" in r.message]
    assert len(drop_lines) == 4
    totals = [int(m.split("total_drops=")[1]) for m in drop_lines]
    assert totals == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_finding_cooldown_log_no_app_id_renders_dash(tmp_path, caplog):
    """When app_id is None, the log renders ``app=-`` (not ``app=None``)
    — keeps grep patterns stable across app-available vs not."""
    import logging
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    await sensor._ingest_frame(_make_frame(dhash="1111111111111111", app_id=None))
    with caplog.at_level(logging.INFO, logger="Ouroboros.VisionSensor"):
        await sensor._ingest_frame(_make_frame(dhash="2222222222222222", app_id=None))

    drop_lines = [r.message for r in caplog.records if "dropped finding_cooldown" in r.message]
    assert len(drop_lines) == 1
    assert "app=-" in drop_lines[0]


@pytest.mark.asyncio
async def test_finding_cooldown_allows_different_app(tmp_path):
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    s1 = await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="X"))
    s2 = await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="Y"))
    assert s1 is not None
    assert s2 is not None


@pytest.mark.asyncio
async def test_finding_cooldown_allows_different_match_set(tmp_path):
    # Two calls with different OCR text produce different match sets
    # (traceback vs linter_red) — cooldown does not fire.
    outputs = iter([
        "Traceback (most recent call last):",
        "TypeError: cannot concatenate",
    ])
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: next(outputs),
    )
    s1 = await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="X"))
    s2 = await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="X"))
    assert s1 is not None
    assert s2 is not None


@pytest.mark.asyncio
async def test_finding_cooldown_expires_after_window(tmp_path):
    sensor = _make_sensor(
        tmp_path,
        finding_cooldown_s=0.05,    # 50 ms — tight test window
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    s1 = await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="X"))
    await asyncio.sleep(0.08)
    s2 = await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="X"))
    assert s1 is not None
    assert s2 is not None
    assert sensor.stats.dropped_finding_cooldown == 0


def test_finding_cooldown_disabled_when_zero(tmp_path):
    sensor = _make_sensor(tmp_path, finding_cooldown_s=0.0)
    # Even an explicit mark + active=check cycle says "not active".
    sensor._mark_finding_emitted(
        verdict="error_visible", app_id="X", matches=("traceback",),
    )
    assert not sensor._finding_cooldown_active(
        verdict="error_visible", app_id="X", matches=("traceback",),
    )


# ---------------------------------------------------------------------------
# Chain cap
# ---------------------------------------------------------------------------


def test_chain_cap_default_is_one(tmp_path):
    sensor = _make_sensor(tmp_path)
    # Default from spec: one governed chain per session until trust builds.
    assert sensor._chain_max == 1
    assert sensor.chain_budget_remaining == 1


def test_chain_start_pauses_at_cap(tmp_path):
    sensor = _make_sensor(tmp_path, chain_max=1)
    sensor.record_chain_start("op-1")
    assert sensor.chain_budget_remaining == 0
    assert sensor.paused is True
    assert sensor.pause_reason == PAUSE_REASON_CHAIN_CAP


def test_chain_start_idempotent(tmp_path):
    # Same op_id twice is a no-op (the orchestrator may re-stamp).
    sensor = _make_sensor(tmp_path, chain_max=2)
    sensor.record_chain_start("op-1")
    sensor.record_chain_start("op-1")
    assert sensor.chain_budget_remaining == 1
    assert sensor.paused is False


def test_chain_cap_of_three_allows_two_before_pause(tmp_path):
    sensor = _make_sensor(tmp_path, chain_max=3)
    sensor.record_chain_start("op-1")
    sensor.record_chain_start("op-2")
    assert sensor.paused is False
    assert sensor.chain_budget_remaining == 1
    sensor.record_chain_start("op-3")
    assert sensor.paused is True
    assert sensor.pause_reason == PAUSE_REASON_CHAIN_CAP


def test_record_chain_start_rejects_empty_op_id(tmp_path):
    sensor = _make_sensor(tmp_path)
    with pytest.raises(ValueError):
        sensor.record_chain_start("")


# ---------------------------------------------------------------------------
# Consecutive-failure penalty
# ---------------------------------------------------------------------------


def test_three_consecutive_failures_pauses_with_penalty(tmp_path):
    sensor = _make_sensor(tmp_path, penalty_s=300.0, fp_budget=1.0)  # budget deactivated
    sensor.record_outcome(op_id="f1", outcome=OUTCOME_REJECTED)
    sensor.record_outcome(op_id="f2", outcome=OUTCOME_REJECTED)
    assert sensor.paused is False
    sensor.record_outcome(op_id="f3", outcome=OUTCOME_REJECTED)
    assert sensor.paused is True
    assert sensor.pause_reason == PAUSE_REASON_CONSECUTIVE_FAILURES
    assert sensor._pause_until_ts is not None


def test_consecutive_failures_reset_by_applied_green(tmp_path):
    sensor = _make_sensor(tmp_path, fp_budget=1.0)
    sensor.record_outcome(op_id="f1", outcome=OUTCOME_REJECTED)
    sensor.record_outcome(op_id="f2", outcome=OUTCOME_STALE)
    sensor.record_outcome(op_id="t1", outcome=OUTCOME_APPLIED_GREEN)
    # Streak back to 0 after a TP.
    assert sensor._consecutive_failures == 0
    sensor.record_outcome(op_id="f3", outcome=OUTCOME_REJECTED)
    assert sensor.paused is False
    assert sensor._consecutive_failures == 1


def test_consecutive_failures_uncertain_does_not_reset(tmp_path):
    sensor = _make_sensor(tmp_path, fp_budget=1.0)
    sensor.record_outcome(op_id="f1", outcome=OUTCOME_REJECTED)
    sensor.record_outcome(op_id="u1", outcome=OUTCOME_UNCERTAIN)
    # Streak still at 1 — uncertain neither bumps nor resets.
    assert sensor._consecutive_failures == 1


def test_consecutive_failure_pause_auto_expires(tmp_path):
    sensor = _make_sensor(tmp_path, penalty_s=0.01, fp_budget=1.0)  # 10 ms penalty
    for i in range(3):
        sensor.record_outcome(op_id=f"f{i}", outcome=OUTCOME_REJECTED)
    assert sensor.paused is True
    time.sleep(0.03)
    # is_paused() transparently auto-resumes once the wall-clock
    # deadline passes.
    assert sensor.paused is False
    assert sensor._pause_reason == ""


# ---------------------------------------------------------------------------
# Persistence + restart behavior
# ---------------------------------------------------------------------------


def test_ledger_persists_across_restart(tmp_path):
    s1 = _make_sensor(tmp_path, fp_budget=1.0)
    s1.record_outcome(op_id="op-1", outcome=OUTCOME_REJECTED)
    s1.record_outcome(op_id="op-2", outcome=OUTCOME_APPLIED_GREEN)
    # Fresh sensor reads from disk.
    s2 = _make_sensor(tmp_path, fp_budget=1.0)
    assert len(s2._outcomes) == 2
    outcomes = [o.outcome for o in s2._outcomes]
    assert outcomes == [OUTCOME_REJECTED, OUTCOME_APPLIED_GREEN]


def test_finding_cooldowns_persist_across_restart(tmp_path):
    s1 = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    asyncio.run(s1._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="X")))
    assert s1._finding_cooldowns  # populated
    s2 = _make_sensor(
        tmp_path,
        finding_cooldown_s=120.0,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    # s2 inherits the cooldown — so its first attempt on the same shape
    # is dropped.
    env = asyncio.run(s2._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="X")))
    assert env is None
    assert s2.stats.dropped_finding_cooldown == 1


def test_consecutive_failures_persists_across_restart(tmp_path):
    s1 = _make_sensor(tmp_path, fp_budget=1.0)
    s1.record_outcome(op_id="f1", outcome=OUTCOME_REJECTED)
    s1.record_outcome(op_id="f2", outcome=OUTCOME_REJECTED)
    # Second sensor sees the streak.
    s2 = _make_sensor(tmp_path, fp_budget=1.0)
    assert s2._consecutive_failures == 2
    # One more failure trips the 3-consecutive pause.
    s2.record_outcome(op_id="f3", outcome=OUTCOME_REJECTED)
    assert s2.paused is True
    assert s2.pause_reason == PAUSE_REASON_CONSECUTIVE_FAILURES


def test_pause_state_does_NOT_persist_across_restart(tmp_path):
    """Spec §Policy Layer: operator gets a fresh budget window on boot."""
    s1 = _make_sensor(tmp_path, fp_budget=1.0)
    s1._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    # New sensor — pause state cleared regardless of on-disk ledger.
    s2 = _make_sensor(tmp_path, fp_budget=1.0)
    assert s2.paused is False
    assert s2._pause_reason == ""


def test_chain_tracker_does_not_persist(tmp_path):
    """Chain cap is per-session — survival across restart would lock
    out the operator forever."""
    s1 = _make_sensor(tmp_path, chain_max=1)
    s1.record_chain_start("op-yesterday")
    assert s1.paused is True
    s2 = _make_sensor(tmp_path, chain_max=1)
    assert s2.paused is False
    assert s2.chain_budget_remaining == 1


def test_corrupted_ledger_ignored_silently(tmp_path):
    ledger = tmp_path / ".jarvis" / "vision_sensor_fp_ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{not valid json", encoding="utf-8")
    sensor = _make_sensor(tmp_path)
    # No crash, empty state.
    assert list(sensor._outcomes) == []
    assert sensor._finding_cooldowns == {}


def test_malformed_outcome_entries_dropped(tmp_path):
    ledger = tmp_path / ".jarvis" / "vision_sensor_fp_ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({
        "outcomes": [
            {"op_id": "x", "outcome": "rejected", "ts": 1.0},         # valid
            {"op_id": "y", "outcome": "wrong", "ts": 2.0},            # bad outcome
            {"outcome": "applied_green", "ts": 3.0},                  # missing op_id
            "not a dict",                                             # wrong type
            {"op_id": "z", "outcome": "applied_green", "ts": "NaN"},  # bad ts type
        ],
    }), encoding="utf-8")
    sensor = _make_sensor(tmp_path)
    assert len(sensor._outcomes) == 1
    assert sensor._outcomes[0].op_id == "x"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_resume_clears_pause_and_chain_tracker(tmp_path):
    sensor = _make_sensor(tmp_path, chain_max=1)
    sensor.record_chain_start("op-1")
    assert sensor.paused is True
    sensor.resume()
    assert sensor.paused is False
    assert sensor._pause_reason == ""
    assert sensor.chain_budget_remaining == 1


def test_resume_does_not_clear_outcomes_ledger(tmp_path):
    sensor = _make_sensor(tmp_path, fp_budget=1.0)
    sensor.record_outcome(op_id="x", outcome=OUTCOME_REJECTED)
    sensor.resume()
    # Resume is pause-only; historical audit trail stays intact.
    assert len(sensor._outcomes) == 1


# ---------------------------------------------------------------------------
# Input validation on record_outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_outcome", ["", "GREEN", "success", "failed", "not-valid"],
)
def test_record_outcome_rejects_unknown_outcome(tmp_path, bad_outcome):
    sensor = _make_sensor(tmp_path)
    with pytest.raises(ValueError):
        sensor.record_outcome(op_id="x", outcome=bad_outcome)


def test_record_outcome_rejects_empty_op_id(tmp_path):
    sensor = _make_sensor(tmp_path)
    with pytest.raises(ValueError):
        sensor.record_outcome(op_id="", outcome=OUTCOME_APPLIED_GREEN)


# ---------------------------------------------------------------------------
# Rolling window maxlen respected
# ---------------------------------------------------------------------------


def test_rolling_window_drops_oldest_on_overflow(tmp_path):
    sensor = _make_sensor(tmp_path, fp_window_size=5, fp_budget=1.0)
    for i in range(10):
        sensor.record_outcome(op_id=f"op-{i}", outcome=OUTCOME_APPLIED_GREEN)
    assert len(sensor._outcomes) == 5
    # Last five are op-5..op-9
    assert [o.op_id for o in sensor._outcomes] == [f"op-{i}" for i in range(5, 10)]
