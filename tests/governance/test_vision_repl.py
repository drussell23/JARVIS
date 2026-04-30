"""Regression spine for Task 21 — `/vision` REPL + dashboard renderers.

Scope:

* ``handle_vision_status`` — not-configured path; detail lines for
  armed / paused (every pause reason) / boost-active sensor states.
* ``handle_vision_resume`` — idempotent; reports which pause reason
  was cleared.
* ``handle_vision_boost`` — TTY-gate refusal, parse errors,
  clamp-to-300s reporting, deliberate disable of cost-cascade +
  clearing cost-cap pause on activation.
* ``format_vision_status_line`` — single-line dashboard render for
  every state (off / armed / paused / boosted).
* ``vision_origin_tag`` — prefix only for ``vision_sensor`` source.

Sensor-side additions (boost state, persistence, cascade gating) are
covered by targeted tests driving the real ``VisionSensor``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
    PAUSE_REASON_CHAIN_CAP,
    PAUSE_REASON_COST_CAP,
    PAUSE_REASON_FP_BUDGET,
    FrameData,
    VisionSensor,
)
from backend.core.ouroboros.governance.vision_repl import (
    _default_tty_check,
    format_vision_status_line,
    handle_vision_boost,
    handle_vision_resume,
    handle_vision_status,
    vision_origin_tag,
)


# ---------------------------------------------------------------------------
# Autouse isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield


class _StubRouter:
    async def ingest(self, envelope):
        return "enqueued"


def _make_sensor(tmp_path, *, chain_max=3, fp_window_size=20, **kw):
    return VisionSensor(
        router=_StubRouter(),
        session_id="repl-test",
        retention_root=str(tmp_path / ".jarvis" / "vision_frames"),
        frame_ttl_s=0.0,
        register_shutdown_hooks=False,
        ledger_path=str(tmp_path / ".jarvis" / "vision_sensor_fp_ledger.json"),
        cost_ledger_path=str(tmp_path / ".jarvis" / "vision_cost_ledger.json"),
        chain_max=chain_max,
        fp_window_size=fp_window_size,
        **kw,
    )


# ---------------------------------------------------------------------------
# handle_vision_status
# ---------------------------------------------------------------------------


def test_status_not_configured_when_sensor_is_none():
    out = handle_vision_status(None)
    assert "not configured" in out
    assert "JARVIS_VISION_SENSOR_ENABLED" in out


def test_status_armed_sensor_shows_core_fields(tmp_path):
    sensor = _make_sensor(tmp_path)
    out = handle_vision_status(sensor)
    assert out.startswith("vision: armed")
    assert "tier2:" in out
    assert "chain:" in out
    assert "signals:" in out
    assert "tier2_calls:" in out
    assert "FP rate:" in out
    assert "today:" in out
    assert "boost: off" in out


def test_status_paused_shows_reason_token(tmp_path):
    sensor = _make_sensor(tmp_path)
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    out = handle_vision_status(sensor)
    assert f"reason={PAUSE_REASON_FP_BUDGET}" in out


def test_status_chain_cap_pause_reason(tmp_path):
    sensor = _make_sensor(tmp_path, chain_max=1)
    sensor.record_chain_start("op-1")
    out = handle_vision_status(sensor)
    assert f"reason={PAUSE_REASON_CHAIN_CAP}" in out
    assert "chain: 0/1" in out


def test_status_fp_rate_none_when_window_not_full(tmp_path):
    sensor = _make_sensor(tmp_path, fp_window_size=20)
    out = handle_vision_status(sensor)
    assert "FP rate: n/a" in out


def test_status_fp_rate_computed_when_window_full(tmp_path):
    from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
        OUTCOME_APPLIED_GREEN, OUTCOME_REJECTED,
    )

    sensor = _make_sensor(tmp_path, fp_window_size=4)
    sensor.record_outcome(op_id="a", outcome=OUTCOME_APPLIED_GREEN)
    sensor.record_outcome(op_id="b", outcome=OUTCOME_APPLIED_GREEN)
    sensor.record_outcome(op_id="c", outcome=OUTCOME_APPLIED_GREEN)
    sensor.record_outcome(op_id="d", outcome=OUTCOME_REJECTED)
    # 1 FP / 4 total = 25%
    out = handle_vision_status(sensor)
    assert "FP rate: 25" in out


def test_status_boost_active_shows_remaining(tmp_path):
    sensor = _make_sensor(tmp_path)
    sensor.enable_boost(120.0)
    out = handle_vision_status(sensor)
    assert "boost: active" in out
    assert "remaining" in out


def test_status_cost_shown_with_cap(tmp_path):
    sensor = _make_sensor(tmp_path, daily_cost_cap_usd=1.00)
    sensor._cost_today_usd = 0.25
    out = handle_vision_status(sensor)
    assert "$0.2500" in out
    assert "$1.00" in out


# ---------------------------------------------------------------------------
# handle_vision_resume
# ---------------------------------------------------------------------------


def test_resume_none_sensor():
    assert "not configured" in handle_vision_resume(None)


def test_resume_armed_sensor_idempotent(tmp_path):
    sensor = _make_sensor(tmp_path)
    out = handle_vision_resume(sensor)
    assert "already armed" in out


def test_resume_paused_sensor_clears_state(tmp_path):
    sensor = _make_sensor(tmp_path)
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    assert sensor.paused is True
    out = handle_vision_resume(sensor)
    assert "resumed" in out
    assert PAUSE_REASON_FP_BUDGET in out
    assert sensor.paused is False


def test_resume_reports_each_pause_reason(tmp_path):
    for reason in (
        PAUSE_REASON_FP_BUDGET,
        PAUSE_REASON_CHAIN_CAP,
        PAUSE_REASON_COST_CAP,
    ):
        sensor = _make_sensor(tmp_path)
        sensor._pause(reason=reason, duration_s=None)
        out = handle_vision_resume(sensor)
        assert reason in out


# ---------------------------------------------------------------------------
# handle_vision_boost — TTY-gated, clamped
# ---------------------------------------------------------------------------


def test_boost_refuses_in_non_tty(tmp_path):
    sensor = _make_sensor(tmp_path)
    out = handle_vision_boost(sensor, "60", tty_check_fn=lambda: False)
    assert "refused" in out
    assert "non-interactive" in out or "headless" in out


def test_boost_rejects_wrong_arg_count(tmp_path):
    sensor = _make_sensor(tmp_path)
    tty = lambda: True
    for bad in ("", "a b", "   "):
        out = handle_vision_boost(sensor, bad, tty_check_fn=tty)
        assert "usage:" in out.lower()


def test_boost_rejects_non_numeric(tmp_path):
    sensor = _make_sensor(tmp_path)
    out = handle_vision_boost(sensor, "abc", tty_check_fn=lambda: True)
    assert "invalid seconds" in out


def test_boost_rejects_zero_or_negative(tmp_path):
    sensor = _make_sensor(tmp_path)
    tty = lambda: True
    assert "must be > 0" in handle_vision_boost(sensor, "0", tty_check_fn=tty)
    assert "must be > 0" in handle_vision_boost(sensor, "-30", tty_check_fn=tty)


def test_boost_enables_and_reports_duration(tmp_path):
    sensor = _make_sensor(tmp_path)
    out = handle_vision_boost(sensor, "60", tty_check_fn=lambda: True)
    assert "active for 60s" in out
    assert sensor.is_boost_active() is True


def test_boost_clamps_above_300s_and_reports(tmp_path):
    sensor = _make_sensor(tmp_path)
    out = handle_vision_boost(sensor, "900", tty_check_fn=lambda: True)
    assert "clamped to 300s" in out
    assert "active for 300s" in out
    assert sensor.boost_remaining_s() <= 300.0


def test_boost_none_sensor():
    assert "not configured" in handle_vision_boost(None, "60", tty_check_fn=lambda: True)


def test_boost_clears_cost_cap_pause(tmp_path):
    """Enabling a boost on a cost-cap-paused sensor clears the pause —
    the operator has explicitly accepted the spend."""
    sensor = _make_sensor(tmp_path, daily_cost_cap_usd=1.00)
    sensor._pause(reason=PAUSE_REASON_COST_CAP, duration_s=None)
    assert sensor.paused is True
    handle_vision_boost(sensor, "60", tty_check_fn=lambda: True)
    assert sensor.paused is False


def test_boost_does_not_clear_other_pause_reasons(tmp_path):
    """Boost only clears cost_cap pauses — FP budget / chain cap need
    an explicit `/vision resume`."""
    sensor = _make_sensor(tmp_path)
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    handle_vision_boost(sensor, "60", tty_check_fn=lambda: True)
    assert sensor.paused is True
    assert sensor.pause_reason == PAUSE_REASON_FP_BUDGET


# ---------------------------------------------------------------------------
# Boost behavior in cost cascade (sensor-side wiring)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boost_suppresses_cost_downshift(tmp_path):
    """Cost at 80% normally skips Tier 2 VLM; boost overrides."""
    sensor = _make_sensor(
        tmp_path,
        vlm_fn=lambda _p: {
            "verdict": "bug_visible", "confidence": 0.9,
            "model": "qwen3-vl-235b",
        },
        tier2_enabled=True,
        tier2_cost_usd=0.005,
        daily_cost_cap_usd=1.00,
        ocr_fn=lambda _p: "",
        finding_cooldown_s=0.0,
    )
    # Pre-load spend to 80% → downshift active.
    sensor._cost_today_usd = 0.80
    assert sensor._cost_downshift_active() is True
    # Enable boost — downshift suppressed.
    sensor.enable_boost(60.0)
    assert sensor._cost_downshift_active() is False


def test_boost_suppresses_95_percent_pause(tmp_path):
    """With boost active, crossing 95% doesn't trigger cost-cap pause."""
    sensor = _make_sensor(
        tmp_path,
        tier2_cost_usd=1.00,           # one call = 100%
        daily_cost_cap_usd=1.00,
    )
    sensor.enable_boost(60.0)
    assert sensor.is_boost_active() is True
    # Simulate a VLM call's spend.
    sensor._record_tier2_spend()
    # Spend now at $1.00 = 100% > 95% threshold, but boost suppressed.
    assert sensor.paused is False


def test_boost_state_persists_across_restart(tmp_path):
    """Disk-persisted: a sensor restart within the boost window still
    sees the boost as active."""
    s1 = _make_sensor(tmp_path)
    s1.enable_boost(60.0)
    # Fresh sensor loads the persisted cost ledger, which now carries
    # a boost_until_ts.
    s2 = _make_sensor(tmp_path)
    assert s2.is_boost_active() is True
    assert s2.boost_remaining_s() > 0


def test_stale_boost_not_restored_on_restart(tmp_path):
    """A boost_until_ts that has already passed isn't restored."""
    s1 = _make_sensor(tmp_path)
    s1.enable_boost(60.0)
    # Fake an expired deadline on disk.
    s1._boost_until_ts = time.time() - 60.0
    s1._persist_cost_ledger()
    s2 = _make_sensor(tmp_path)
    assert s2.is_boost_active() is False
    assert s2._boost_until_ts is None


def test_boost_remaining_zero_when_inactive(tmp_path):
    sensor = _make_sensor(tmp_path)
    assert sensor.is_boost_active() is False
    assert sensor.boost_remaining_s() == 0.0


def test_boost_enable_clamps_short_values_up_to_one(tmp_path):
    sensor = _make_sensor(tmp_path)
    granted = sensor.enable_boost(0.1)    # below minimum
    assert granted == 1.0


# ---------------------------------------------------------------------------
# format_vision_status_line — single-line dashboard render
# ---------------------------------------------------------------------------


def test_status_line_none_sensor():
    assert format_vision_status_line(None) == "vision: off"


def test_status_line_armed(tmp_path):
    sensor = _make_sensor(tmp_path, daily_cost_cap_usd=1.00)
    line = format_vision_status_line(sensor)
    assert line.startswith("vision: armed ")
    assert "today=$0.0000" in line
    assert "$1.00" in line


def test_status_line_paused_with_reason(tmp_path):
    sensor = _make_sensor(tmp_path)
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    line = format_vision_status_line(sensor)
    assert f"paused reason={PAUSE_REASON_FP_BUDGET}" in line


def test_status_line_boost_token_present_when_active(tmp_path):
    sensor = _make_sensor(tmp_path)
    sensor.enable_boost(120.0)
    line = format_vision_status_line(sensor)
    assert "boost=" in line
    # Boost token appears between state and today= tokens.
    assert line.index("boost=") < line.index("today=")


def test_status_line_fits_single_line(tmp_path):
    """Regression guard: the status line contains no newlines —
    SerpentFlow renders it inline as a single line."""
    sensor = _make_sensor(tmp_path)
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    sensor.enable_boost(60.0)
    line = format_vision_status_line(sensor)
    assert "\n" not in line


# ---------------------------------------------------------------------------
# vision_origin_tag
# ---------------------------------------------------------------------------


def test_vision_origin_tag_matches():
    assert vision_origin_tag("vision_sensor") == "[vision-origin] "


def test_vision_origin_tag_case_insensitive():
    assert vision_origin_tag("VISION_SENSOR") == "[vision-origin] "
    assert vision_origin_tag("  vision_sensor  ") == "[vision-origin] "


@pytest.mark.parametrize("src", [
    "test_failure", "voice_human", "ai_miner", "backlog", "", None,
])
def test_vision_origin_tag_empty_for_non_vision_sources(src):
    assert vision_origin_tag(src) == ""


# ---------------------------------------------------------------------------
# Default TTY check returns a bool in all envs (sanity)
# ---------------------------------------------------------------------------


def test_default_tty_check_returns_bool():
    result = _default_tty_check()
    assert isinstance(result, bool)
