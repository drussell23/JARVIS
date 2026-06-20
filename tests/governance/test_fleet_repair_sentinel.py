from __future__ import annotations
import re
import pytest
from backend.core.ouroboros.governance.fleet_repair_battery import BATTERY
from backend.core.ouroboros.governance.fleet_evaluator import ProbeResult
from backend.core.ouroboros.governance import fleet_calibration_store as s
from backend.core.ouroboros.governance.fleet_repair_sentinel import RepairSentinel


_ARITH = next(d for d in BATTERY if d.name == "arithmetic")


def _first_block(prompt):
    m = re.search(r"```python\s*(.*?)```", prompt, re.S)
    return m.group(1) if m else ""


def _smart_caller(fix=True):
    """Prompt-aware fake: fixes the (possibly mutated/renamed) function it's
    given, mirroring a real model. fix=False returns the buggy code unchanged."""
    async def call(model, messages, *, max_tokens):
        buggy = _first_block(messages[-1]["content"])
        # operator fix, robust to the mutator's param renaming (a -> a_vXXXX)
        out = buggy.replace(" - ", " + ", 1) if fix else buggy
        return ProbeResult(text="```python\n" + out + "```", ttft_ms=100.0,
                           total_ms=1000.0, completion_tokens=60, ok=True, error="")
    return call


def _store(tmp_path):
    import os
    os.environ["JARVIS_FLEET_CALIBRATION_PATH"] = str(tmp_path / "c.json")
    return s.FleetCalibrationStore()


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_ENABLED", "false")
    calls = []
    async def spy(m, msgs, *, max_tokens):
        calls.append(m); return ProbeResult("", 0, 0, 0, False, "")
    sen = RepairSentinel(model_caller=spy, store=_store(tmp_path),
                         defects=(_ARITH,), monitored_models=("m",))
    assert await sen.maybe_run_sentinel(now=1.0) == 0
    assert calls == []          # gated off -> zero DW calls


@pytest.mark.asyncio
async def test_not_idle_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_DEFECTS_PER_CYCLE", "1")
    sen = RepairSentinel(model_caller=_smart_caller(), store=_store(tmp_path),
                         idle_check=lambda: False, defects=(_ARITH,),
                         monitored_models=("m",))
    assert await sen.maybe_run_sentinel(now=1.0) == 0


@pytest.mark.asyncio
async def test_passing_repair_records_code_pass_true(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_DEFECTS_PER_CYCLE", "1")
    st = _store(tmp_path)
    sen = RepairSentinel(model_caller=_smart_caller(fix=True), store=st,
                         idle_check=lambda: True, clock=lambda: 1.0,
                         defects=(_ARITH,), monitored_models=("dw-pro",))
    probed = await sen.maybe_run_sentinel(now=1.0)
    assert probed == 1
    sc = st.score("dw-pro")
    assert sc is not None and sc.ast_pass_rate > 0.9    # healthy -> high EWMA


@pytest.mark.asyncio
async def test_failing_repair_records_demote_signal(monkeypatch, tmp_path):
    """Auto-demote signal: a DW model that FAILS the synthetic repair drives its
    ast_pass_rate EWMA down (feeds the existing rerank/graduation demotion)."""
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_SENTINEL_DEFECTS_PER_CYCLE", "1")
    st = _store(tmp_path)
    st.record_probe("dw-pro", kind="code", code_pass=True, ttft_ms=100, tok_per_s=50, now=1.0)
    sen = RepairSentinel(model_caller=_smart_caller(fix=False), store=st,
                         idle_check=lambda: True, clock=lambda: 2.0,
                         defects=(_ARITH,), monitored_models=("dw-pro",))
    await sen.maybe_run_sentinel(now=2.0)
    sc = st.score("dw-pro")
    assert sc.ast_pass_rate < 1.0      # degraded by the failed synthetic repair
