# tests/governance/test_fleet_evaluator.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import fleet_evaluator as fe
from backend.core.ouroboros.governance import fleet_calibration_store as s


def _fake_caller(behavior):
    async def call(model_id, messages, *, max_tokens):
        is_code = "code block" in messages[-1]["content"].lower()
        if behavior == "good":
            text = "```python\ndef merge_intervals(x):\n    '''m'''\n    return sorted(x)\n```" if is_code else "ENRICH"
            return fe.ProbeResult(text=text, ttft_ms=200, total_ms=1000, completion_tokens=80, ok=True, error="")
        if behavior == "prose":  # the 397B failure mode
            return fe.ProbeResult(text="Let me think about intervals...", ttft_ms=150, total_ms=4000, completion_tokens=900, ok=True, error="")
        return fe.ProbeResult(text="", ttft_ms=0, total_ms=0, completion_tokens=0, ok=False, error="HTTP 502")
    return call


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "false")
    assert fe.fleet_evaluator_enabled() is False


def test_authoritative_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", raising=False)
    assert fe.fleet_authoritative_enabled() is False


@pytest.mark.asyncio
async def test_good_model_scores_high(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    ev = fe.FleetEvaluator(model_caller=_fake_caller("good"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0)
    await ev.calibrate_models(["deepseek"])
    sc = store.score("deepseek")
    assert sc.ast_pass_rate > 0.9 and sc.label_adherence > 0.9


@pytest.mark.asyncio
async def test_prose_model_scores_zero_ast(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    ev = fe.FleetEvaluator(model_caller=_fake_caller("prose"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0)
    await ev.calibrate_models(["qwen397"])
    assert store.score("qwen397").ast_pass_rate < 0.1   # the diagnosed bug, now measured


@pytest.mark.asyncio
async def test_502_is_failsoft(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    ev = fe.FleetEvaluator(model_caller=_fake_caller("502"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0)
    await ev.calibrate_models(["devstral"])     # must NOT raise
    assert store.score("devstral").ast_pass_rate == 0.0


@pytest.mark.asyncio
async def test_maybe_calibrate_skips_when_not_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    calls = []
    async def spy(model_id, messages, *, max_tokens):
        calls.append(model_id); return fe.ProbeResult("", 0, 0, 0, False, "")
    ev = fe.FleetEvaluator(model_caller=spy, store=store, idle_check=lambda: False,
                           clock=lambda: 1.0, snapshot_loader=lambda: ["m"])
    await ev.maybe_calibrate(now=1.0)
    assert calls == []     # not idle -> no probes


@pytest.mark.asyncio
async def test_maybe_calibrate_disabled_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    calls = []
    async def spy(model_id, messages, *, max_tokens):
        calls.append(model_id); return fe.ProbeResult("", 0, 0, 0, False, "")
    ev = fe.FleetEvaluator(model_caller=spy, idle_check=lambda: True,
                           clock=lambda: 1.0, snapshot_loader=lambda: ["m"])
    await ev.maybe_calibrate(now=1.0)
    assert calls == []     # master OFF -> no probes


@pytest.mark.asyncio
async def test_graduation_flips_after_stable_cycles(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("JARVIS_FLEET_GRAD_MIN_SAMPLES", "1")
    monkeypatch.setenv("JARVIS_FLEET_GRAD_STABLE_CYCLES", "2")
    store = s.FleetCalibrationStore()
    # pre-seed: a measured-bad default + a measured-good coder
    store.record_probe("qwen397", kind="code", code_pass=False, ttft_ms=150, tok_per_s=120, now=1.0)
    store.record_probe("deepseek", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=1.0)
    flips = []
    ev = fe.FleetEvaluator(model_caller=_fake_caller("good"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0,
                           default_model="qwen397",
                           flag_persister=lambda name, val: flips.append((name, val)))
    ev._maybe_graduate(now=1.0)   # cycle 1 -> proposes, not yet stable
    assert flips == []
    ev._maybe_graduate(now=2.0)   # cycle 2 -> stable -> flip
    assert flips and flips[-1][0] == "JARVIS_FLEET_EVALUATOR_AUTHORITATIVE" and flips[-1][1] == "true"
